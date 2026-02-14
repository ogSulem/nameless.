from __future__ import annotations

import logging
import os

from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Complaint, Dialog, Message, Photo, User
from app.telegram_safe import safe_send_message, safe_send_photo
from app.utils.markdown import escape_markdown

logger = logging.getLogger(__name__)


class ComplaintService:
    async def create_and_notify(
        self,
        bot: Bot,
        session: AsyncSession,
        admins: set[int],
        media_root: str,
        dialog_id: int,
        from_tg_id: int,
        reason: str,
    ) -> None:
        # Actually, let's keep it simple and just optimize the queries here since create_and_notify 
        # is already called with a session.
        res = await session.execute(select(User.id).where(User.telegram_id == from_tg_id))
        from_user_id = res.scalar_one_or_none()
        if from_user_id is None:
            return

        res_d = await session.execute(
            select(Dialog.user1_id, Dialog.user2_id)
            .where(Dialog.id == dialog_id)
        )
        dialog_row = res_d.one_or_none()
        accused_tg_id: int | None = None
        if dialog_row is not None:
            u1_id, u2_id = dialog_row
            accused_user_id = u2_id if u1_id == from_user_id else u1_id
            res_acc = await session.execute(select(User.telegram_id).where(User.id == accused_user_id))
            accused_tg_id = res_acc.scalar_one_or_none()
            accused_tg_id = int(accused_tg_id) if accused_tg_id else None

        async def _tg_label(tg_id: int) -> str:
            try:
                chat = await bot.get_chat(tg_id)
                full_name = (getattr(chat, "full_name", None) or "").strip()
                username = (getattr(chat, "username", None) or "").strip()
                uname = f"@{username}" if username else ""
                name_part = f"{full_name} " if full_name else ""
                return f"{name_part}{uname} (id: `{tg_id}`, tg://user?id={tg_id})".strip()
            except Exception:
                return f"id: `{tg_id}` (tg://user?id={tg_id})"

        session.add(Complaint(dialog_id=dialog_id, from_user_id=from_user_id, reason=reason))
        await session.commit()

        msg_rows = await session.execute(
            select(Message.id, Message.text, Message.created_at, Message.photo_id, Message.from_user_id)
            .where(Message.dialog_id == dialog_id)
            .order_by(Message.created_at.asc())
        )
        messages = msg_rows.all()

        photo_rows = await session.execute(select(Photo.file_path).where(Photo.dialog_id == dialog_id))
        photos = [r[0] for r in photo_rows.all()]

        # Prepare report - simplified to avoid character limits
        reporter_line = await _tg_label(from_tg_id)
        accused_line = await _tg_label(accused_tg_id) if accused_tg_id else "не найден"
        text_lines = [
            "🚨 *ЖАЛОБА*",
            f"Диалог: `{dialog_id}`",
            f"Отправитель: {reporter_line}",
            f"На кого: {accused_line}",
            f"Причина: {escape_markdown(reason)}",
            "",
            "*Лог сообщений:*",
        ]
        
        # Limit to last 15 messages to prevent Telegram limits
        for mid, txt, created_at, photo_id, f_user_id in messages[-15:]:
            time_str = created_at.strftime("%H:%M:%S")
            sender = "Вы" if f_user_id == from_user_id else "Партнер"
            content = txt if txt else f"📷 Фото (id: {photo_id})"
            text_lines.append(f"• `{time_str}` [{sender}]: {escape_markdown(content)}")

        report = "\n".join(text_lines)

        for admin_id in admins:
            try:
                # 1. Send text report with markdown
                await safe_send_message(bot, admin_id, report, parse_mode="Markdown", disable_web_page_preview=True)
                
                # 2. Send photos if any
                for p in photos:
                    tg_file_id = self._extract_tg_file_id(p)
                    if tg_file_id:
                        await safe_send_photo(bot, admin_id, photo=tg_file_id)
                        continue

                    abs_path = self._resolve_media_path(media_root=media_root, stored_path=p)
                    if abs_path and os.path.exists(abs_path):
                        await safe_send_photo(bot, admin_id, photo=FSInputFile(abs_path))
            except Exception:
                logger.exception("FAILED_SEND_COMPLAINT admin=%s", admin_id)

        logger.info("complaint_created dialog_id=%s from=%s", dialog_id, from_tg_id)

    def _resolve_media_path(self, media_root: str, stored_path: str) -> str | None:
        if not stored_path:
            return None

        p = stored_path.replace("\\", "/")

        if p.startswith("/media/"):
            rel = p.removeprefix("/media/")
            return os.path.join(media_root, rel)

        return os.path.join(media_root, p.lstrip("/"))

    def _extract_tg_file_id(self, stored_path: str) -> str | None:
        if not stored_path:
            return None

        p = stored_path.strip()
        if not p.startswith("tg://"):
            return None

        file_id = p.removeprefix("tg://").strip()
        return file_id or None
