from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape

from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Complaint, ComplaintStatus, Dialog, User
from app.keyboards.admin import complaint_card_kb
from app.telegram_safe import safe_send_message

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CreatedComplaint:
    complaint_id: int
    accused_tg: int | None
    dialog_id: int


class ComplaintService:
    """Stores complaints and pings the moderation chat.

    Legal posture: photos and message excerpts are **not** broadcast anywhere by
    default. Moderators get a short alert with buttons and open the dialog inside
    the admin panel, which writes an audit record for every view.
    """

    async def create(
        self,
        session: AsyncSession,
        *,
        dialog_id: int,
        from_tg_id: int,
        reason: str,
    ) -> CreatedComplaint | None:
        res = await session.execute(select(User).where(User.telegram_id == int(from_tg_id)))
        reporter = res.scalar_one_or_none()
        if reporter is None:
            return None

        res_d = await session.execute(
            select(Dialog.user1_id, Dialog.user2_id).where(Dialog.id == dialog_id)
        )
        row = res_d.one_or_none()
        accused_db_id: int | None = None
        if row is not None:
            u1, u2 = int(row[0]), int(row[1])
            accused_db_id = u2 if u1 == reporter.id else u1

        accused_tg: int | None = None
        if accused_db_id is not None:
            res_a = await session.execute(select(User.telegram_id).where(User.id == accused_db_id))
            accused_tg = res_a.scalar_one_or_none()
            accused_tg = int(accused_tg) if accused_tg else None

        complaint = Complaint(
            dialog_id=dialog_id,
            from_user_id=reporter.id,
            accused_user_id=accused_db_id,
            reason=reason[:4000],
            status=ComplaintStatus.open.value,
        )
        session.add(complaint)
        await session.commit()

        logger.info("complaint_created id=%s dialog=%s from=%s", complaint.id, dialog_id, from_tg_id)
        return CreatedComplaint(int(complaint.id), accused_tg, dialog_id)

    async def notify_moderators(
        self,
        bot: Bot,
        redis: Redis,
        settings: Settings,
        created: CreatedComplaint,
        reason: str,
    ) -> None:
        text = "\n".join(
            [
                f"<b>🚨 Жалоба #{created.complaint_id}</b>",
                f"Диалог: <code>{created.dialog_id}</code>",
                f"На пользователя: <code>{created.accused_tg if created.accused_tg else '—'}</code>",
                "",
                f"Причина: {escape(reason[:300])}",
                "",
                "<i>Переписка и фото доступны только в админ-панели (/admin).</i>",
            ]
        )
        kb = complaint_card_kb(complaint_id=created.complaint_id, accused_tg=created.accused_tg)

        targets = set(settings.alerts_target_ids) | set(settings.admin_ids) | set(settings.moderator_ids)
        for target in targets:
            try:
                await safe_send_message(bot, int(target), text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                logger.warning("complaint_notify_failed target=%s", target, exc_info=True)

        # Fallback: last complaint is always visible in the admin panel, so a
        # failed notification never means a lost report.
        try:
            await redis.set("mod:last_complaint", str(created.complaint_id), ex=60 * 60 * 24 * 7)
        except Exception:
            pass
