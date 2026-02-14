from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy import case, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.exc import DBAPIError

from app.database.models import ActiveDialog, Dialog, DialogStatus, Message as DbMessage, Photo

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ActiveDialogInfo:
    dialog_id: int
    partner_telegram_id: int
    has_photos: bool


class DialogService:
    def __init__(self, media_root: str) -> None:
        self._media_root = media_root

    async def get_active_dialog(self, session: AsyncSession, dialog_id: int, me_telegram_id: int) -> ActiveDialogInfo | None:
        me_user_id = await self._user_id_by_tg(session, me_telegram_id)
        if me_user_id is None:
            return None

        from app.database.models import User

        partner_user_id_expr = case(
            (Dialog.user1_id == me_user_id, Dialog.user2_id),
            else_=Dialog.user1_id,
        )
        Partner = aliased(User)

        res = await session.execute(
            select(Dialog.id, Dialog.has_photos, Partner.telegram_id)
            .join(Partner, Partner.id == partner_user_id_expr)
            .where(Dialog.id == dialog_id)
            .where(Dialog.status == DialogStatus.active)
            .where(Dialog.user1_id != Dialog.user2_id)
            .where((Dialog.user1_id == me_user_id) | (Dialog.user2_id == me_user_id))
        )
        row = res.one_or_none()
        if row is None:
            return None

        _dialog_id, has_photos, partner_tg = row
        return ActiveDialogInfo(dialog_id=int(_dialog_id), partner_telegram_id=int(partner_tg), has_photos=bool(has_photos))

    async def save_text(self, session: AsyncSession, dialog_id: int, from_user_id: int, text: str) -> None:
        last_exc: Exception | None = None
        for i in range(2):
            try:
                session.add(DbMessage(dialog_id=dialog_id, from_user_id=from_user_id, text=text))
                await session.commit()
                return
            except DBAPIError as e:
                last_exc = e
                try:
                    await session.rollback()
                except Exception:
                    pass
                if i == 0:
                    await asyncio.sleep(0.2)
                continue
            except Exception as e:
                try:
                    await session.rollback()
                except Exception:
                    pass
                raise e

        assert last_exc is not None
        raise last_exc

    async def save_photo(self, bot: Bot, session: AsyncSession, dialog_id: int, owner_user_id: int, tg_id: int, msg: Message) -> str:
        if not msg.photo:
            raise ValueError("no photo")

        # Just store the file_id in the database, no local download
        photo = msg.photo[-1]
        file_id = photo.file_id

        last_exc: Exception | None = None
        for i in range(2):
            try:
                db_photo = Photo(dialog_id=dialog_id, owner_user_id=owner_user_id, file_path=f"tg://{file_id}")
                session.add(db_photo)
                await session.flush()

                session.add(DbMessage(dialog_id=dialog_id, from_user_id=owner_user_id, photo_id=db_photo.id))

                dialog = await session.get(Dialog, dialog_id)
                if dialog is not None and not dialog.has_photos:
                    dialog.has_photos = True

                await session.commit()
                logger.info("photo_reference_saved dialog_id=%s owner_tg=%s file_id=%s", dialog_id, tg_id, file_id)
                return file_id
            except DBAPIError as e:
                last_exc = e
                try:
                    await session.rollback()
                except Exception:
                    pass
                if i == 0:
                    await asyncio.sleep(0.2)
                continue
            except Exception as e:
                try:
                    await session.rollback()
                except Exception:
                    pass
                raise e

        assert last_exc is not None
        raise last_exc

    async def finish_dialog(self, session: AsyncSession, dialog_id: int) -> Dialog | None:
        dialog = await session.get(Dialog, dialog_id)
        if dialog is None:
            return None
        if dialog.status != DialogStatus.active:
            # Already finished or in other terminal state
            return dialog

        dialog.status = DialogStatus.finished
        dialog.finished_at = datetime.now(tz=timezone.utc)

        # Remove from ActiveDialog table to allow users to search again
        await session.execute(delete(ActiveDialog).where(ActiveDialog.dialog_id == dialog_id))
        
        try:
            await session.commit()
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            raise
        return dialog

    async def _user_id_by_tg(self, session: AsyncSession, tg_id: int) -> int | None:
        from app.database.models import User

        res = await session.execute(select(User.id).where(User.telegram_id == tg_id))
        return res.scalar_one_or_none()

    async def _tg_id_by_user_id(self, session: AsyncSession, user_id: int) -> int:
        from app.database.models import User

        res = await session.execute(select(User.telegram_id).where(User.id == user_id))
        return int(res.scalar_one())
