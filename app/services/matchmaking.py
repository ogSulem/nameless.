from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import ActiveDialog, Dialog, DialogStatus, User
from app.redis import keys
from app.redis.lua import RELEASE_LUA, RESERVE_LUA

logger = logging.getLogger(__name__)

PREMIUM_CACHE_TTL_S = 60
MAX_RESERVE_ATTEMPTS = 25
PREMIUM_PROBE = 6
# Premium profiles are pushed to the front of the queue (~2 minutes of head start).
PREMIUM_QUEUE_BOOST_S = 120
# Nobody wants the same stranger three times in a row.
RECENT_PARTNERS_MAX = 3
RECENT_TTL_S = 60 * 60 * 24 * 7
# After a complaint the pair is never matched again (both directions).
NO_MATCH_TTL_S = 60 * 60 * 24 * 30
DEFAULT_AGE_MIN = 18
DEFAULT_AGE_MAX = 99


def age_from(birth_date: date | None) -> int:
    if birth_date is None:
        return DEFAULT_AGE_MIN
    today = datetime.now(tz=UTC).date()
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))


@dataclass(frozen=True, slots=True)
class MatchFilters:
    """Who a user wants to meet. Only Premium users get to change it."""

    age_min: int = DEFAULT_AGE_MIN
    age_max: int = DEFAULT_AGE_MAX
    city_only: bool = False

    def fits(self, *, age: int, my_city: str | None, their_city: str | None) -> bool:
        if not self.age_min <= age <= self.age_max:
            return False
        if self.city_only and my_city:
            if not their_city:
                return True  # "вся страна" still counts: we cannot ask them to move
            return my_city.strip().lower() == their_city.strip().lower()
        return True


@dataclass(frozen=True, slots=True)
class MatchResult:
    dialog_id: int
    partner_telegram_id: int
    partner_db_id: int


@dataclass(frozen=True, slots=True)
class _Profile:
    """The searching side, flattened: what they are and what they accept."""

    age: int
    gender: str
    city: str | None
    premium: bool
    filters: MatchFilters


@dataclass(frozen=True, slots=True)
class _Candidate:
    telegram_id: int
    db_id: int
    queue_key: str
    score: str
    rating: float = 0.0
    ratings_count: int = 0
    appearance: float = 0.0
    appearance_count: int = 0

    def metric(self, mode: str) -> tuple[float, int]:
        if mode == "appearance":
            return self.appearance, self.appearance_count
        return self.rating, self.ratings_count


def seek_gender_for(gender: str) -> str:
    return "female" if gender == "male" else "male"


class MatchmakingService:
    """Redis-fast, DB-consistent matchmaking.

    * Queues are ZSETs scored by enqueue time (FIFO, fair).
    * Reserving a candidate is atomic (Lua) and TTL-bounded.
    * The database is the source of truth; Redis is only an accelerator.
    * Every failure path returns the candidate to the queue instead of losing them:
      losing a waiting user was one of the worst behaviours of the previous version.
    """

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # ------------------------------------------------------------------ online
    async def mark_online(self, tg_id: int) -> None:
        try:
            await self._redis.set(keys.online_key(int(tg_id)), "1", ex=int(self._settings.online_ttl_s))
        except Exception:
            logger.warning("mark_online_failed tg=%s", tg_id, exc_info=True)

    async def is_online(self, tg_id: int) -> bool:
        try:
            return bool(await self._redis.exists(keys.online_key(int(tg_id))))
        except Exception:
            return True  # a Redis hiccup must not stop matching

    # ----------------------------------------------------------------- premium
    async def invalidate_premium(self, telegram_id: int) -> None:
        try:
            await self._redis.delete(_premium_key(telegram_id))
        except Exception:
            pass

    async def is_premium(self, session: AsyncSession, user: User) -> bool:
        key = _premium_key(int(user.telegram_id))
        try:
            cached = _as_str(await self._redis.get(key))
            if cached is not None:
                return cached == "1"
        except Exception:
            pass

        until = user.subscription_until
        active = False
        if until is not None:
            aware = until if until.tzinfo else until.replace(tzinfo=UTC)
            active = aware > datetime.now(tz=UTC)
        try:
            await self._redis.set(key, "1" if active else "0", ex=PREMIUM_CACHE_TTL_S)
        except Exception:
            pass
        return active

    # ------------------------------------------------------------------- queue
    async def enqueue(self, session: AsyncSession, user: User) -> str:
        """Join the city queue *and* the country-wide queue.

        Someone who picked "Казань" must still be findable by a Muscovite when
        nobody local is around — otherwise small cities never match at all.
        """
        qkey = keys.queue_key(user.gender.value, user.city)
        global_key = keys.queue_key(user.gender.value, None)
        now = datetime.now(tz=UTC).timestamp()
        tg = int(user.telegram_id)
        score = now - PREMIUM_QUEUE_BOOST_S if _is_premium_now(user) else now

        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.zadd(qkey, {str(tg): score})
            if global_key != qkey:
                pipe.zadd(global_key, {str(tg): score})
            pipe.set(keys.queue_member_key(tg), _member_payload(user), ex=int(self._settings.online_ttl_s))
            pipe.set(
                keys.seek_gender_key(tg),
                seek_gender_for(user.gender.value),
                ex=60 * 60 * 24,
            )
            await pipe.execute()
        except Exception:
            logger.exception("enqueue_failed tg=%s", tg)
            return qkey

        await self.mark_online(tg)
        logger.info("queue_add tg=%s queue=%s", tg, qkey)
        return qkey

    async def dequeue(self, telegram_id: int) -> None:
        tg = int(telegram_id)
        member_key = keys.queue_member_key(tg)
        try:
            payload = _as_str(await self._redis.get(member_key))
            if payload:
                gender, _, city = payload.partition("|")
                pipe = self._redis.pipeline(transaction=False)
                pipe.zrem(keys.queue_key(gender, city or None), str(tg))
                pipe.zrem(keys.queue_key(gender, None), str(tg))
                await pipe.execute()
            await self._redis.delete(member_key)
        except Exception:
            logger.warning("dequeue_failed tg=%s", tg, exc_info=True)

    async def queue_size(self, gender: str, city: str | None = None) -> int:
        """How many people are waiting for this gender right now."""
        try:
            return int(await self._redis.zcard(keys.queue_key(gender, city)))
        except Exception:
            return 0

    # ------------------------------------------------------------------- match
    async def premium_mode(self, telegram_id: int) -> str:
        """What a Premium user asked to prioritise: `appearance` or `chat`."""
        try:
            raw = _as_str(await self._redis.get(keys.premium_pref_key(int(telegram_id))))
        except Exception:
            return "chat"
        return "appearance" if raw == "appearance" else "chat"

    async def set_premium_mode(self, telegram_id: int, mode: str) -> str:
        mode = "appearance" if mode == "appearance" else "chat"
        try:
            await self._redis.set(keys.premium_pref_key(int(telegram_id)), mode, ex=60 * 60 * 24 * 365)
        except Exception:
            logger.warning("premium_mode_save_failed tg=%s", telegram_id, exc_info=True)
        return mode

    async def try_match(self, session: AsyncSession, user: User, premium: bool) -> MatchResult | None:
        tg = int(user.telegram_id)

        if await self._has_active_dialog(session, tg, user.id):
            return None

        # Leave the queue while scanning (also refreshes "online").
        await self.dequeue(tg)
        await self.mark_online(tg)

        seek = await self._seek_gender(tg, user.gender.value)
        queues: list[str] = []
        if user.city:
            queues.append(keys.queue_key(seek, user.city))
        queues.append(keys.queue_key(seek, None))

        mode = await self.premium_mode(tg) if premium else "chat"
        profile = _Profile(
            age=age_from(user.birth_date),
            gender=getattr(user.gender, "value", user.gender),
            city=user.city,
            premium=premium,
            filters=MatchFilters(
                age_min=int(user.filter_age_min or DEFAULT_AGE_MIN),
                age_max=int(user.filter_age_max or DEFAULT_AGE_MAX),
                city_only=bool(user.filter_city_only),
            )
            if premium
            else MatchFilters(),
        )
        candidate = await self._pick_candidate(session, user, queues, premium, mode, profile)
        if candidate is None:
            await self.enqueue(session, user)
            logger.info("match_enqueued tg=%s premium=%s queues=%s", tg, premium, queues)
            return None

        dialog_id = await self._create_dialog(session, user, candidate)
        if dialog_id is None:
            await self.enqueue(session, user)
            return None

        await self._publish_active(dialog_id, tg, candidate.telegram_id)
        await self.remember_pair(tg, candidate.telegram_id)
        logger.info(
            "match_created dialog_id=%s u1=%s u2=%s premium=%s rating=%.2f",
            dialog_id, tg, candidate.telegram_id, premium, candidate.rating,
        )
        return MatchResult(
            dialog_id=dialog_id,
            partner_telegram_id=candidate.telegram_id,
            partner_db_id=candidate.db_id,
        )

    async def _has_active_dialog(self, session: AsyncSession, tg: int, db_id: int) -> bool:
        try:
            if await self._redis.exists(keys.active_dialog(tg)):
                return True
        except Exception:
            pass
        try:
            return await session.get(ActiveDialog, db_id) is not None
        except Exception:
            logger.warning("active_dialog_check_failed tg=%s", tg, exc_info=True)
            return False

    async def _seek_gender(self, tg: int, my_gender: str) -> str:
        try:
            pref = _as_str(await self._redis.get(keys.seek_gender_key(tg)))
        except Exception:
            pref = None
        if pref in {"male", "female"}:
            return pref
        return seek_gender_for(my_gender)

    async def _pick_candidate(
        self,
        session: AsyncSession,
        user: User,
        queues: list[str],
        premium: bool,
        mode: str = "chat",
        profile: _Profile | None = None,
    ) -> _Candidate | None:
        """Pick a partner with ~3 round-trips per batch instead of ~3 per candidate.

        High load used to mean: for every candidate in the queue a DB query, a
        Redis call for "is online", a Redis call for "what do they seek" and
        another DB query for an active dialog. Now a batch of candidates is
        reserved first, then their state is fetched with one Redis pipeline and
        two SQL queries.
        """
        me = int(user.telegram_id)
        my_gender = getattr(user.gender, "value", user.gender)
        checked: set[int] = set()
        reserved: list[_Candidate] = []
        skipped: list[_Candidate] = []
        chosen: _Candidate | None = None

        try:
            last_partner = await self._last_partner(me)
            recent, hidden = await self._exclusions(me)
            batch_size = max(int(PREMIUM_PROBE), 4) if premium else 4
            attempts = 0

            while attempts < 3:
                attempts += 1
                raws = await self._reserve_batch(queues, me, batch_size)
                fresh: list[_Candidate] = []
                for raw in raws:
                    if raw.telegram_id in checked or raw.telegram_id in {me, last_partner}:
                        skipped.append(raw)
                        continue
                    # Same stranger again, or somebody this user reported: not a match.
                    if raw.telegram_id in recent or raw.telegram_id in hidden:
                        skipped.append(raw)
                        continue
                    checked.add(raw.telegram_id)
                    fresh.append(raw)
                if not fresh:
                    break

                ids = [c.telegram_id for c in fresh]
                hints = await self._candidate_hints(ids)
                rows = await self._load_candidates(session, ids)
                busy = await self._active_db_ids(session, [r.db_id for r in rows.values()])

                for raw in fresh:
                    online, seek, redis_busy = hints.get(raw.telegram_id, (True, None, False))
                    if not online:
                        await self._release(raw)  # ghost entry: drop it
                        continue

                    row = rows.get(raw.telegram_id)
                    if row is None or row.db_id == user.id or row.is_banned or row.is_deleted:
                        await self._release(raw)
                        continue

                    cand = _Candidate(
                        telegram_id=raw.telegram_id,
                        db_id=row.db_id,
                        queue_key=raw.queue_key,
                        score=raw.score,
                        rating=row.rating,
                        ratings_count=row.ratings_count,
                        appearance=row.appearance,
                        appearance_count=row.appearance_count,
                    )

                    # Mutual consent: they must be looking for my gender as well,
                    # otherwise a woman seeking women could be matched with a man.
                    candidate_seek = seek or seek_gender_for(row.gender)
                    if candidate_seek != my_gender:
                        skipped.append(cand)
                        continue

                    # Filters work both ways: mine must accept them, theirs must
                    # accept me. Otherwise Premium would be a one-sided "show me
                    # only 20-year-olds" switch.
                    if profile is not None:
                        if not profile.filters.fits(
                            age=row.age, my_city=profile.city, their_city=row.city
                        ):
                            skipped.append(cand)
                            continue
                        if row.premium and not row.filters.fits(
                            age=profile.age, my_city=row.city, their_city=profile.city
                        ):
                            skipped.append(cand)
                            continue

                    if redis_busy or row.db_id in busy:
                        await self._release(cand)
                        continue

                    if not premium:
                        chosen = cand
                        return cand

                    if self._qualifies_for_premium(cand, mode):
                        chosen = cand
                        return cand

                    if len(reserved) < int(PREMIUM_PROBE):
                        reserved.append(cand)
                    else:
                        await self._release(cand, requeue=True)

                if not premium or len(reserved) >= int(PREMIUM_PROBE):
                    break

            if premium and reserved:
                # Honest fallback, documented in the terms: best available by the
                # chosen metric, so a Premium user never waits forever.
                chosen = max(reserved, key=lambda c: c.metric(mode))
                return chosen

            return None
        finally:
            for cand in reserved:
                if chosen is not None and cand.telegram_id == chosen.telegram_id:
                    continue
                await self._release(cand, requeue=True)
            for cand in skipped:
                await self._release(cand, requeue=True)

    async def _reserve_batch(self, queues: list[str], me: int, size: int) -> list[_Candidate]:
        out: list[_Candidate] = []
        for _ in range(max(1, size)):
            cand = await self._reserve(queues, me)
            if cand is None:
                break
            out.append(cand)
        return out

    async def _candidate_hints(self, ids: list[int]) -> dict[int, tuple[bool, str | None, bool]]:
        """One Redis round-trip for online/seek/busy of a whole batch."""
        if not ids:
            return {}
        try:
            pipe = self._redis.pipeline(transaction=False)
            for tg in ids:
                pipe.exists(keys.online_key(int(tg)))
                pipe.get(keys.seek_gender_key(int(tg)))
                pipe.exists(keys.active_dialog(int(tg)))
            raw = await pipe.execute()
        except Exception:
            logger.warning("candidate_hints_failed", exc_info=True)
            return {}

        hints: dict[int, tuple[bool, str | None, bool]] = {}
        for index, tg in enumerate(ids):
            online, seek, busy = raw[index * 3 : index * 3 + 3]
            seek_value = _as_str(seek)
            hints[int(tg)] = (
                bool(online),
                seek_value if seek_value in {"male", "female"} else None,
                bool(busy),
            )
        return hints

    async def _load_candidates(
        self, session: AsyncSession, ids: list[int]
    ) -> dict[int, MatchmakingService._Row]:
        if not ids:
            return {}
        try:
            res = await session.execute(
                select(
                    User.id,
                    User.telegram_id,
                    User.is_banned,
                    User.is_deleted,
                    User.season_rating_chat,
                    User.chat_ratings_count,
                    User.season_rating_appearance,
                    User.appearance_ratings_count,
                    User.gender,
                    User.birth_date,
                    User.city,
                    User.subscription_until,
                    User.filter_age_min,
                    User.filter_age_max,
                    User.filter_city_only,
                ).where(User.telegram_id.in_([int(i) for i in ids]))
            )
            rows = res.all()
        except DBAPIError:
            logger.exception("candidates_load_failed")
            return {}

        now = datetime.now(tz=UTC)
        out: dict[int, MatchmakingService._Row] = {}
        for row in rows:
            until = row[11]
            if until is not None:
                aware = until if until.tzinfo else until.replace(tzinfo=UTC)
                premium = aware > now
            else:
                premium = False
            out[int(row[1])] = self._Row(
                db_id=int(row[0]),
                is_banned=bool(row[2]),
                is_deleted=bool(row[3]),
                rating=float(row[4] or 0.0),
                ratings_count=int(row[5] or 0),
                appearance=float(row[6] or 0.0),
                appearance_count=int(row[7] or 0),
                gender=str(getattr(row[8], "value", row[8]) or "male"),
                age=age_from(row[9]),
                city=(str(row[10]) if row[10] else None),
                premium=premium,
                filters=MatchFilters(
                    age_min=int(row[12] or DEFAULT_AGE_MIN),
                    age_max=int(row[13] or DEFAULT_AGE_MAX),
                    city_only=bool(row[14]),
                )
                if premium
                else MatchFilters(),
            )
        return out

    async def _active_db_ids(self, session: AsyncSession, db_ids: list[int]) -> set[int]:
        """Which of these users are already talking to somebody (authoritative check)."""
        ids = [int(i) for i in db_ids]
        if not ids:
            return set()
        try:
            res = await session.execute(
                select(ActiveDialog.user_id).where(ActiveDialog.user_id.in_(ids))
            )
            return {int(x) for x in res.scalars().all()}
        except DBAPIError:
            logger.warning("active_db_ids_failed", exc_info=True)
            return set()

    def _qualifies_for_premium(self, cand: _Candidate, mode: str = "chat") -> bool:
        min_count = int(self._settings.rating_min_count)
        if mode == "appearance":
            return (
                cand.appearance >= float(self._settings.premium_min_appearance)
                and cand.appearance_count >= min_count
            )
        return (
            cand.rating >= float(self._settings.premium_min_rating)
            and cand.ratings_count >= min_count
        )

    async def _reserve(self, queues: list[str], me: int) -> _Candidate | None:
        now = datetime.now(tz=UTC).timestamp()
        try:
            raw = await self._redis.eval(
                RESERVE_LUA, 0, str(me), f"{now:.3f}", "5000", str(MAX_RESERVE_ATTEMPTS), *queues
            )
        except Exception:
            logger.exception("reserve_lua_failed me=%s", me)
            return None

        if not raw or len(raw) < 3:
            return None

        return _Candidate(
            telegram_id=int(raw[0]),
            db_id=0,
            queue_key=_as_str(raw[1]) or "",
            score=_as_str(raw[2]) or str(int(now)),
        )

    async def _release(self, cand: _Candidate, requeue: bool = False) -> None:
        try:
            await self._redis.eval(RELEASE_LUA, 1, keys.lock_match(cand.telegram_id), str(cand.telegram_id))
        except Exception:
            logger.warning("release_lock_failed tg=%s", cand.telegram_id, exc_info=True)

        if requeue and cand.queue_key:
            try:
                await self._redis.zadd(cand.queue_key, {str(cand.telegram_id): float(cand.score)})
            except Exception:
                logger.warning("requeue_failed tg=%s", cand.telegram_id, exc_info=True)

    async def _exclusions(self, me: int) -> tuple[set[int], set[int]]:
        """Recent partners + reported pairs, one round-trip."""
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.lrange(keys.recent_partners_key(me), 0, RECENT_PARTNERS_MAX - 1)
            pipe.smembers(keys.no_match_key(me))
            recent_raw, hidden_raw = await pipe.execute()
        except Exception:
            logger.warning("exclusions_failed tg=%s", me, exc_info=True)
            return set(), set()

        def to_int(values) -> set[int]:
            out: set[int] = set()
            for value in values or []:
                try:
                    out.add(int(value))
                except (TypeError, ValueError):
                    continue
            return out

        return to_int(recent_raw), to_int(hidden_raw)

    async def remember_pair(self, first_tg: int, second_tg: int) -> None:
        """Both sides remember the other, so the queue does not loop the same pair."""
        try:
            pipe = self._redis.pipeline(transaction=False)
            for me, other in ((first_tg, second_tg), (second_tg, first_tg)):
                key = keys.recent_partners_key(int(me))
                pipe.lpush(key, str(int(other)))
                pipe.ltrim(key, 0, RECENT_PARTNERS_MAX - 1)
                pipe.expire(key, RECENT_TTL_S)
            await pipe.execute()
        except Exception:
            logger.warning("remember_pair_failed a=%s b=%s", first_tg, second_tg, exc_info=True)

    async def block_pair(self, first_tg: int, second_tg: int) -> None:
        """Called after a complaint: these two never meet again."""
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.sadd(keys.no_match_key(int(first_tg)), str(int(second_tg)))
            pipe.expire(keys.no_match_key(int(first_tg)), NO_MATCH_TTL_S)
            pipe.sadd(keys.no_match_key(int(second_tg)), str(int(first_tg)))
            pipe.expire(keys.no_match_key(int(second_tg)), NO_MATCH_TTL_S)
            await pipe.execute()
        except Exception:
            logger.warning("block_pair_failed a=%s b=%s", first_tg, second_tg, exc_info=True)

    async def _last_partner(self, me: int) -> int | None:
        try:
            raw = _as_str(await self._redis.get(keys.last_partner(me)))
        except Exception:
            return None
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    @dataclass(frozen=True, slots=True)
    class _Row:
        db_id: int
        is_banned: bool
        is_deleted: bool
        rating: float
        ratings_count: int
        appearance: float = 0.0
        appearance_count: int = 0
        gender: str = "male"
        age: int = DEFAULT_AGE_MIN
        city: str | None = None
        premium: bool = False
        filters: MatchFilters = MatchFilters()

    async def _create_dialog(self, session: AsyncSession, user: User, cand: _Candidate) -> int | None:
        try:
            dialog = Dialog(user1_id=user.id, user2_id=cand.db_id, status=DialogStatus.active)
            session.add(dialog)
            await session.flush()
            session.add_all(
                [
                    ActiveDialog(user_id=user.id, dialog_id=dialog.id),
                    ActiveDialog(user_id=cand.db_id, dialog_id=dialog.id),
                ]
            )
            await session.commit()
            return int(dialog.id)
        except IntegrityError:
            await session.rollback()
            await self._release(cand, requeue=True)
            logger.info("match_race_lost cand=%s", cand.telegram_id)
            return None
        except DBAPIError:
            await session.rollback()
            await self._release(cand, requeue=True)
            logger.exception("match_create_failed my_tg=%s cand=%s", user.telegram_id, cand.telegram_id)
            return None

    async def _publish_active(self, dialog_id: int, my_tg: int, partner_tg: int) -> None:
        ttl = int(self._settings.dialog_max_hours) * 3600
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.set(keys.active_dialog(my_tg), str(dialog_id), ex=ttl)
            pipe.set(keys.active_dialog(partner_tg), str(dialog_id), ex=ttl)
            pipe.set(keys.dialog_partner_tg(dialog_id, my_tg), str(partner_tg), ex=ttl)
            pipe.set(keys.dialog_partner_tg(dialog_id, partner_tg), str(my_tg), ex=ttl)
            for tg in (my_tg, partner_tg):
                for k in (
                    keys.pending_rating(tg),
                    keys.pending_rating_has_photos(tg),
                    keys.pending_rating_partner(tg),
                    keys.pending_rating_action(tg),
                    keys.pending_rating_step(tg),
                ):
                    pipe.delete(k)
                pipe.delete(keys.ui_search_message_id(tg))
            await pipe.execute()
        except Exception:
            # The DB already knows about the dialog; DialogService repairs the cache.
            logger.exception("publish_active_failed dialog_id=%s", dialog_id)


def _is_premium_now(user: User) -> bool:
    until = getattr(user, "subscription_until", None)
    if until is None:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until > datetime.now(tz=UTC)


def _member_payload(user: User) -> str:
    return f"{user.gender.value}|{user.city or ''}"


def _premium_key(tg_id: int) -> str:
    return f"user:premium:{int(tg_id)}"


def _as_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)
