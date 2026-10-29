from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

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


@dataclass(frozen=True, slots=True)
class MatchResult:
    dialog_id: int
    partner_telegram_id: int
    partner_db_id: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    telegram_id: int
    db_id: int
    queue_key: str
    score: str
    rating: float = 0.0
    ratings_count: int = 0


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

        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.zadd(qkey, {str(tg): now})
            if global_key != qkey:
                pipe.zadd(global_key, {str(tg): now})
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

    async def queue_size(self, gender: str, city: str | None) -> int:
        try:
            return int(await self._redis.zcard(keys.queue_key(gender, city)))
        except Exception:
            return 0

    # ------------------------------------------------------------------- match
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

        candidate = await self._pick_candidate(session, user, queues, premium)
        if candidate is None:
            await self.enqueue(session, user)
            logger.info("match_enqueued tg=%s premium=%s queues=%s", tg, premium, queues)
            return None

        dialog_id = await self._create_dialog(session, user, candidate)
        if dialog_id is None:
            await self.enqueue(session, user)
            return None

        await self._publish_active(dialog_id, tg, candidate.telegram_id)
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
    ) -> _Candidate | None:
        me = int(user.telegram_id)
        checked: set[int] = set()
        reserved: list[_Candidate] = []
        skipped: list[_Candidate] = []
        chosen: _Candidate | None = None

        try:
            last_partner = await self._last_partner(me)

            for _ in range(MAX_RESERVE_ATTEMPTS):
                raw = await self._reserve(queues, me)
                if raw is None:
                    break

                if raw.telegram_id in checked:
                    skipped.append(raw)
                    continue
                checked.add(raw.telegram_id)

                # Same partner as the previous dialog: keep them queued for others.
                if raw.telegram_id == last_partner:
                    skipped.append(raw)
                    continue

                if not await self.is_online(raw.telegram_id):
                    await self._release(raw)  # ghost entry: drop it
                    continue

                row = await self._load_candidate(session, raw.telegram_id)
                if row is None or row.db_id == user.id:
                    await self._release(raw)
                    continue

                cand = _Candidate(
                    telegram_id=raw.telegram_id,
                    db_id=row.db_id,
                    queue_key=raw.queue_key,
                    score=raw.score,
                    rating=row.rating,
                    ratings_count=row.ratings_count,
                )

                if row.is_banned or row.is_deleted:
                    await self._release(cand)
                    continue

                if await session.get(ActiveDialog, cand.db_id) is not None:
                    await self._release(cand)
                    continue

                if not premium:
                    chosen = cand
                    return cand

                reserved.append(cand)
                if self._qualifies_for_premium(cand):
                    chosen = cand
                    return cand
                if len(reserved) >= PREMIUM_PROBE:
                    break

            if premium and reserved:
                # Honest fallback, documented in the terms: best available.
                chosen = max(reserved, key=lambda c: (c.rating, c.ratings_count))
                return chosen

            return None
        finally:
            for cand in reserved:
                if chosen is not None and cand.telegram_id == chosen.telegram_id:
                    continue
                await self._release(cand, requeue=True)
            for cand in skipped:
                await self._release(cand, requeue=True)

    def _qualifies_for_premium(self, cand: _Candidate) -> bool:
        return (
            cand.rating >= float(self._settings.premium_min_rating)
            and cand.ratings_count >= int(self._settings.rating_min_count)
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

    async def _load_candidate(self, session: AsyncSession, tg_id: int) -> MatchmakingService._Row | None:
        try:
            res = await session.execute(
                select(
                    User.id,
                    User.is_banned,
                    User.is_deleted,
                    User.season_rating_chat,
                    User.chat_ratings_count,
                ).where(User.telegram_id == int(tg_id))
            )
            row = res.one_or_none()
        except DBAPIError:
            logger.exception("candidate_load_failed tg=%s", tg_id)
            return None
        if row is None:
            return None
        return self._Row(
            db_id=int(row[0]),
            is_banned=bool(row[1]),
            is_deleted=bool(row[2]),
            rating=float(row[3] or 0.0),
            ratings_count=int(row[4] or 0),
        )

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
