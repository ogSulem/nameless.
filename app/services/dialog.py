from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import ActiveDialog, Dialog, DialogStatus, Photo, User
from app.database.models import Message as DbMessage
from app.redis import keys

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DialogInfo:
    dialog_id: int
    me_db_id: int
    me_telegram_id: int
    partner_db_id: int
    partner_telegram_id: int
    has_photos: bool


class DialogService:
    """Dialog lifecycle with the database as the source of truth.

    Redis holds only a cache of "who is chatting with whom". If the cache is lost
    (restart, eviction, TTL), every read falls back to `active_dialogs` and the
    cache is repopulated. The previous implementation deadlocked users for good
    after 12 hours because the Redis key silently expired while the DB row stayed.
    """

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    @property
    def ttl_s(self) -> int:
        return max(60, int(self._settings.dialog_max_hours) * 3600)

    # ------------------------------------------------------------------ lookup
    async def active_dialog_id(self, session: AsyncSession, telegram_id: int) -> int | None:
        tg = int(telegram_id)
        cached = await self._cache_get_int(keys.active_dialog(tg))
        if cached is not None:
            # Heartbeat: an active conversation must never expire from the cache.
            await self._touch(keys.active_dialog(tg))
            return cached

        res = await session.execute(
            select(ActiveDialog.dialog_id)
            .join(User, User.id == ActiveDialog.user_id)
            .where(User.telegram_id == tg)
        )
        dialog_id = res.scalar_one_or_none()
        if dialog_id is None:
            return None

        info = await self.dialog_info(session, int(dialog_id), tg)
        if info is None:
            # Stale row pointing at a finished dialog: clean it up.
            await session.execute(delete(ActiveDialog).where(ActiveDialog.dialog_id == int(dialog_id)))
            await session.commit()
            return None

        await self.publish(info)
        logger.info("active_dialog_cache_repaired tg=%s dialog_id=%s", tg, dialog_id)
        return info.dialog_id

    async def dialog_info(self, session: AsyncSession, dialog_id: int, my_tg: int) -> DialogInfo | None:
        res = await session.execute(
            select(Dialog.user1_id, Dialog.user2_id, Dialog.has_photos, Dialog.status).where(Dialog.id == dialog_id)
        )
        row = res.one_or_none()
        if row is None:
            return None

        user1_id, user2_id, has_photos, status = row
        if status != DialogStatus.active:
            return None

        res_me = await session.execute(select(User.id).where(User.telegram_id == int(my_tg)))
        me_db_id = res_me.scalar_one_or_none()
        if me_db_id is None:
            return None
        me_db_id = int(me_db_id)

        if me_db_id == int(user1_id):
            partner_db_id = int(user2_id)
        elif me_db_id == int(user2_id):
            partner_db_id = int(user1_id)
        else:
            return None

        res_p = await session.execute(select(User.telegram_id).where(User.id == partner_db_id))
        partner_tg = res_p.scalar_one_or_none()
        if partner_tg is None:
            return None

        return DialogInfo(
            dialog_id=int(dialog_id),
            me_db_id=me_db_id,
            me_telegram_id=int(my_tg),
            partner_db_id=partner_db_id,
            partner_telegram_id=int(partner_tg),
            has_photos=bool(has_photos),
        )

    async def partner_telegram_id(self, session: AsyncSession, dialog_id: int, my_tg: int) -> int | None:
        cached = await self._cache_get_int(keys.dialog_partner_tg(dialog_id, int(my_tg)))
        if cached is not None:
            return cached
        info = await self.dialog_info(session, dialog_id, my_tg)
        if info is None:
            return None
        await self.publish(info)
        return info.partner_telegram_id

    # ------------------------------------------------------------------- cache
    async def publish(self, info: DialogInfo) -> None:
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.set(keys.active_dialog(info.me_telegram_id), str(info.dialog_id), ex=self.ttl_s)
            pipe.set(keys.active_dialog(info.partner_telegram_id), str(info.dialog_id), ex=self.ttl_s)
            pipe.set(keys.dialog_partner_tg(info.dialog_id, info.me_telegram_id), str(info.partner_telegram_id),
                     ex=self.ttl_s)
            pipe.set(keys.dialog_partner_tg(info.dialog_id, info.partner_telegram_id), str(info.me_telegram_id),
                     ex=self.ttl_s)
            await pipe.execute()
        except Exception:
            logger.warning("dialog_cache_publish_failed dialog_id=%s", info.dialog_id, exc_info=True)

    async def heartbeat(self, info: DialogInfo) -> None:
        await self.publish(info)

    async def _touch(self, key: str) -> None:
        try:
            await self._redis.expire(key, self.ttl_s)
        except Exception:
            pass

    # ------------------------------------------------------------------- write
    async def save_text(self, session: AsyncSession, dialog_id: int, from_user_id: int, text: str) -> None:
        for attempt in range(2):
            try:
                session.add(DbMessage(dialog_id=dialog_id, from_user_id=from_user_id, text=text))
                await session.commit()
                return
            except DBAPIError:
                await session.rollback()
                if attempt == 0:
                    await asyncio.sleep(0.2)
                    continue
                raise

    async def save_photo(
        self, session: AsyncSession, dialog_id: int, owner_user_id: int, telegram_file_id: str
    ) -> None:
        for attempt in range(2):
            try:
                photo = Photo(dialog_id=dialog_id, owner_user_id=owner_user_id, file_path=f"tg://{telegram_file_id}")
                session.add(photo)
                await session.flush()
                session.add(DbMessage(dialog_id=dialog_id, from_user_id=owner_user_id, photo_id=photo.id))
                dialog = await session.get(Dialog, dialog_id)
                if dialog is not None and not dialog.has_photos:
                    dialog.has_photos = True
                await session.commit()
                return
            except DBAPIError:
                await session.rollback()
                if attempt == 0:
                    await asyncio.sleep(0.2)
                    continue
                raise

    # ------------------------------------------------------------------ finish
    async def finish(self, session: AsyncSession, dialog_id: int, actor_tg_id: int | None = None) -> list[int] | None:
        """Close a dialog, drop the cache, and return both participants' telegram ids."""
        res = await session.execute(
            select(Dialog.user1_id, Dialog.user2_id, Dialog.status).where(Dialog.id == dialog_id)
        )
        row = res.one_or_none()
        if row is None:
            return None

        user1_id, user2_id, status = row
        res_tg = await session.execute(
            select(User.telegram_id).where(User.id.in_([int(user1_id), int(user2_id)]))
        )
        telegram_ids = [int(x) for x in res_tg.scalars().all()]

        if status == DialogStatus.active:
            await session.execute(
                update(Dialog)
                .where(Dialog.id == dialog_id)
                .values(status=DialogStatus.finished, finished_at=datetime.now(tz=UTC))
            )
        await session.execute(delete(ActiveDialog).where(ActiveDialog.dialog_id == dialog_id))
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("finish_dialog_commit_failed dialog_id=%s", dialog_id)
            return None

        await self._drop_cache(dialog_id, telegram_ids)
        logger.info("dialog_finished dialog_id=%s actor=%s tgs=%s", dialog_id, actor_tg_id, telegram_ids)
        return telegram_ids

    async def _drop_cache(self, dialog_id: int, telegram_ids: list[int]) -> None:
        try:
            pipe = self._redis.pipeline(transaction=False)
            for tg in telegram_ids:
                pipe.delete(keys.active_dialog(tg))
                pipe.delete(keys.dialog_partner_tg(dialog_id, tg))
            await pipe.execute()
        except Exception:
            logger.warning("drop_dialog_cache_failed dialog_id=%s", dialog_id, exc_info=True)

    # ------------------------------------------------------------------- sweep
    async def sweep(self, session: AsyncSession, limit: int = 200) -> list[tuple[int, list[int]]]:
        """Close dialogs nobody can end anymore (stale cache, abandoned chat)."""
        cutoff = datetime.now(tz=UTC) - timedelta(hours=int(self._settings.dialog_max_hours))
        res = await session.execute(
            select(Dialog.id, Dialog.created_at)
            .where(Dialog.status == DialogStatus.active)
            .order_by(Dialog.created_at.asc())
            .limit(limit)
        )
        closed: list[tuple[int, list[int]]] = []
        for dialog_id, created_at in res.all():
            created = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
            if created > cutoff:
                continue
            tgs = await self.finish(session, int(dialog_id), actor_tg_id=None)
            if tgs:
                closed.append((int(dialog_id), tgs))
        if closed:
            logger.info("dialog_sweeper_closed count=%s", len(closed))
        return closed

    # ------------------------------------------------------------------- utils
    async def _cache_get_int(self, key: str) -> int | None:
        try:
            raw = await self._redis.get(key)
        except Exception:
            return None
        if raw is None:
            return None
        try:
            return int(raw.decode() if isinstance(raw, bytes) else raw)
        except (TypeError, ValueError):
            return None
