from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.database.models import ActiveDialog, Dialog, DialogStatus, User
from app.redis import keys
from app.redis.lua import MATCH_RESERVE_LUA

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MatchResult:
    dialog_id: int
    partner_user_id: int


class MatchmakingService:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def is_user_premium(self, session: AsyncSession, user_id: int) -> bool:
        try:
            res = await session.execute(select(User.subscription_until).where(User.id == user_id))
            until = res.scalar_one_or_none()
            if until is None:
                return False
            u = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
            return u > datetime.now(tz=timezone.utc)
        except Exception:
            logger.exception("failed_check_premium user_id=%s", user_id)
            return False

    async def enqueue(self, telegram_user_id: int, city: str | None, is_premium_queue: bool) -> str:
        uid = str(telegram_user_id)
        q = self._select_queue(city=city, premium=is_premium_queue)
        await self._redis.lrem(q, 0, uid)
        await self._redis.lpush(q, uid)

        if city is not None:
            qg = self._select_queue(city=None, premium=is_premium_queue)
            await self._redis.lrem(qg, 0, uid)
            await self._redis.lpush(qg, uid)
        return q

    async def dequeue_from_all(self, telegram_user_id: int, city: str | None) -> None:
        uid = str(telegram_user_id)
        for q in {
            keys.queue_global(),
            keys.queue_premium_global(),
            *( {keys.queue_city(city), keys.queue_premium_city(city)} if city else set() ),
        }:
            await self._redis.lrem(q, 0, uid)

    async def try_match(
        self,
        session: AsyncSession,
        user: User,
        premium: bool,
    ) -> MatchResult | None:
        partner_lock_key: str | None = None

        lock_key = keys.lock_match(user.telegram_id)
        lock_ttl_ms = 4000
        got_lock = await self._redis.set(lock_key, "1", nx=True, px=lock_ttl_ms)
        if not got_lock:
            return None

        try:
            if await session.get(ActiveDialog, user.id) is not None:
                return None

            await self.dequeue_from_all(user.telegram_id, user.city)

            city_queues, global_queues = self._queues_for_user(city=user.city, premium=premium)

            max_attempts = 50
            partner: User | None = None

            async def _try_reserve(from_queues: list[str]) -> tuple[str, int | None, str | None]:
                reserved = await self._redis.eval(
                    MATCH_RESERVE_LUA,
                    0,
                    str(user.telegram_id),
                    str(lock_ttl_ms),
                    *from_queues,
                )

                status = reserved[0] if reserved else "NONE"
                if status != "OK":
                    return (status, None, None)
                return (status, int(reserved[1]), str(reserved[2]))

            async def _attempt_match(from_queues: list[str]) -> User | None:
                nonlocal partner_lock_key

                best_candidate: User | None = None
                best_rating: float = -1.0

                for _ in range(max_attempts):
                    if await self._redis.get(keys.active_dialog(user.telegram_id)):
                        return None

                    status, candidate_tg_id, source_queue = await _try_reserve(from_queues)

                    if status in {"NONE", "ACTIVE"}:
                        break

                    if status != "OK" or candidate_tg_id is None or source_queue is None:
                        break

                    partner_lock_key = keys.lock_match(candidate_tg_id)

                    res = await session.execute(select(User).where(User.telegram_id == candidate_tg_id))
                    candidate = res.scalar_one_or_none()
                    
                    if candidate is None or candidate.is_banned:
                        await self._redis.delete(partner_lock_key)
                        partner_lock_key = None
                        continue

                    if await session.get(ActiveDialog, candidate.id) is not None:
                        await self._redis.lpush(source_queue, str(candidate_tg_id))
                        await self._redis.delete(partner_lock_key)
                        partner_lock_key = None
                        continue

                    if await self._redis.get(keys.active_dialog(candidate.telegram_id)):
                        await self._redis.lpush(source_queue, str(candidate_tg_id))
                        await self._redis.delete(partner_lock_key)
                        partner_lock_key = None
                        continue

                    if premium:
                        # Premium users always get the best candidate among checked ones
                        if candidate.season_rating_chat > best_rating:
                            if best_candidate:
                                # Put previous "best" back into queue
                                await self._redis.lpush(source_queue, str(best_candidate.telegram_id))
                                await self._redis.delete(keys.lock_match(best_candidate.telegram_id))
                            best_candidate = candidate
                            best_rating = candidate.season_rating_chat
                        else:
                            # Put back into queue
                            await self._redis.lpush(source_queue, str(candidate_tg_id))
                            await self._redis.delete(partner_lock_key)
                            partner_lock_key = None
                    else:
                        # Non-premium matches with anyone immediately
                        return candidate

                return best_candidate

            partner = None

            if user.city and city_queues:
                partner = await _attempt_match(city_queues)

            if partner is None and global_queues:
                partner = await _attempt_match(global_queues)

            if partner is None:
                await self.enqueue(user.telegram_id, user.city, is_premium_queue=premium)
                logger.info("search_enqueued tg_id=%s premium=%s city=%s", user.telegram_id, premium, user.city)
                return None

            dialog = Dialog(user1_id=user.id, user2_id=partner.id, status=DialogStatus.active)
            session.add(dialog)
            await session.flush()

            session.add_all(
                [
                    ActiveDialog(user_id=user.id, dialog_id=dialog.id),
                    ActiveDialog(user_id=partner.id, dialog_id=dialog.id),
                ]
            )

            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None

            try:
                await self._redis.set(keys.active_dialog(user.telegram_id), str(dialog.id), ex=60 * 60 * 12)
                await self._redis.set(keys.active_dialog(partner.telegram_id), str(dialog.id), ex=60 * 60 * 12)

                # Reset rating/appearance-related state for a fresh dialog.
                # This prevents leaking pending steps/flags between different partners.
                for tg in (user.telegram_id, partner.telegram_id):
                    await self._redis.delete(keys.pending_rating(tg))
                    await self._redis.delete(keys.pending_rating_has_photos(tg))
                    await self._redis.delete(keys.pending_rating_partner(tg))
                    await self._redis.delete(keys.pending_rating_action(tg))
                    await self._redis.delete(keys.pending_rating_step(tg))

                await self._redis.delete(keys.appearance_rating_required(user.telegram_id, dialog.id))
                await self._redis.delete(keys.appearance_rating_required(partner.telegram_id, dialog.id))
            except Exception:
                logger.exception("failed_set_active_dialog_cache user_tg=%s partner_tg=%s", user.telegram_id, partner.telegram_id)

            logger.info(
                "match_created dialog_id=%s u1=%s u2=%s premium=%s",
                dialog.id,
                user.telegram_id,
                partner.telegram_id,
                premium,
            )

            return MatchResult(dialog_id=dialog.id, partner_user_id=partner.telegram_id)
        finally:
            await self._redis.delete(lock_key)
            if partner_lock_key:
                try:
                    await self._redis.delete(partner_lock_key)
                except Exception:
                    logger.exception("failed_release_partner_lock")

    def _queues_for_user(self, city: str | None, premium: bool) -> tuple[list[str], list[str]]:
        if premium:
            if city:
                return (
                    [keys.queue_premium_city(city), keys.queue_city(city)],
                    [keys.queue_premium_global(), keys.queue_global()],
                )
            return ([], [keys.queue_premium_global(), keys.queue_global()])

        if city:
            return ([keys.queue_city(city)], [keys.queue_global()])
        return ([], [keys.queue_global()])

    def _select_queue(self, city: str | None, premium: bool) -> str:
        if premium:
            return keys.queue_premium_city(city) if city else keys.queue_premium_global()
        return keys.queue_city(city) if city else keys.queue_global()
