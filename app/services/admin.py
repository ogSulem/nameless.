from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import (
    AdminAudit,
    Complaint,
    ComplaintStatus,
    Dialog,
    DialogStatus,
    Message,
    Payment,
    Photo,
    Rating,
    User,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- helpers
def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Stats:
    users: int
    new_today: int
    active_24h: int
    dialogs_today: int
    dialogs_active: int
    messages_today: int
    ratings_today: int
    avg_rating: float
    premium: int
    banned: int
    complaints_open: int
    revenue_month_rub: float


def day_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def stats(session: AsyncSession) -> Stats:
    today = day_start()

    async def scalar(stmt) -> int:
        return int((await session.execute(stmt)).scalar_one() or 0)

    users = await scalar(select(func.count(User.id)).where(User.is_deleted.is_(False)))
    new_today = await scalar(select(func.count(User.id)).where(User.created_at >= today))
    active_24h = await scalar(
        select(func.count(User.id)).where(User.last_seen_at >= utcnow() - timedelta(hours=24))
    )
    dialogs_today = await scalar(select(func.count(Dialog.id)).where(Dialog.created_at >= today))
    dialogs_active = await scalar(
        select(func.count(Dialog.id)).where(Dialog.status == DialogStatus.active)
    )
    messages_today = await scalar(select(func.count(Message.id)).where(Message.created_at >= today))
    ratings_today = await scalar(select(func.count(Rating.id)).where(Rating.created_at >= today))
    avg_rating = float(
        (
            await session.execute(
                select(func.avg(User.season_rating_chat)).where(User.chat_ratings_count > 0)
            )
        ).scalar_one()
        or 0.0
    )
    premium = await scalar(
        select(func.count(User.id)).where(User.subscription_until > utcnow())
    )
    banned = await scalar(select(func.count(User.id)).where(User.is_banned.is_(True)))
    complaints_open = await scalar(
        select(func.count(Complaint.id)).where(Complaint.status == ComplaintStatus.open.value)
    )
    revenue = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == "paid", Payment.paid_at >= utcnow() - timedelta(days=30)
            )
        )
    ).scalar_one() or 0

    return Stats(
        users=users,
        new_today=new_today,
        active_24h=active_24h,
        dialogs_today=dialogs_today,
        dialogs_active=dialogs_active,
        messages_today=messages_today,
        ratings_today=ratings_today,
        avg_rating=round(avg_rating, 2),
        premium=premium,
        banned=banned,
        complaints_open=complaints_open,
        revenue_month_rub=round(float(revenue) / 100, 2),
    )


def stats_text(s: Stats, settings: Settings) -> str:
    return (
        "<b>📊 Панель Nameless</b>\n"
        f"<i>{utcnow():%d.%m.%Y %H:%M} UTC</i>\n\n"
        f"👥 Пользователи: <b>{s.users}</b> (+{s.new_today} сегодня)\n"
        f"🟢 Активны за 24ч: <b>{s.active_24h}</b>\n\n"
        f"💬 Диалогов сегодня: <b>{s.dialogs_today}</b> · сейчас: <b>{s.dialogs_active}</b>\n"
        f"✉️ Сообщений сегодня: <b>{s.messages_today}</b>\n"
        f"⭐ Оценок сегодня: <b>{s.ratings_today}</b> · средний рейтинг: <b>{s.avg_rating:.2f}</b>\n\n"
        f"💎 Premium: <b>{s.premium}</b> · выручка 30д: <b>{s.revenue_month_rub:.0f} ₽</b>\n"
        f"🚫 Забанено: <b>{s.banned}</b> · 🚨 Открытых жалоб: <b>{s.complaints_open}</b>"
    )


# ------------------------------------------------------------------------ audit
async def log_action(
    session: AsyncSession,
    *,
    admin_tg: int,
    action: str,
    target_tg: int | None = None,
    details: str | None = None,
) -> None:
    """Every privileged action is recorded: this is the legal safety net for moderation access."""
    try:
        session.add(
            AdminAudit(
                admin_tg=int(admin_tg),
                action=str(action)[:64],
                target_tg=int(target_tg) if target_tg is not None else None,
                details=(details or "")[:2000] or None,
            )
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("admin_audit_failed action=%s admin=%s", action, admin_tg)


# -------------------------------------------------------------------- user card
@dataclass(frozen=True, slots=True)
class UserCard:
    user: User
    text: str
    open_complaints: int
    total_complaints: int
    dialogs: int
    messages: int
    payments_rub: float
    intent: str  # what this user is looking for


async def find_user(session: AsyncSession, arg: int) -> User | None:
    res = await session.execute(select(User).where(User.telegram_id == int(arg)))
    user = res.scalar_one_or_none()
    if user is not None:
        return user
    if 0 < arg < 10_000_000:
        res = await session.execute(select(User).where(User.id == int(arg)))
        return res.scalar_one_or_none()
    return None


def age_of(birth) -> int:
    """Whole years between `birth` (date or datetime) and today, UTC."""
    today = utcnow().date()
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


async def user_card(
    session: AsyncSession, user: User, settings: Settings, intent: str = ""
) -> UserCard:
    open_c = int(
        (
            await session.execute(
                select(func.count(Complaint.id)).where(
                    Complaint.accused_user_id == user.id,
                    Complaint.status == ComplaintStatus.open.value,
                )
            )
        ).scalar_one()
        or 0
    )
    total_c = int(
        (
            await session.execute(
                select(func.count(Complaint.id)).where(Complaint.accused_user_id == user.id)
            )
        ).scalar_one()
        or 0
    )
    dialogs = int(
        (
            await session.execute(
                select(func.count(Dialog.id)).where(
                    (Dialog.user1_id == user.id) | (Dialog.user2_id == user.id)
                )
            )
        ).scalar_one()
        or 0
    )
    paid = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.telegram_id == user.telegram_id, Payment.status == "paid"
            )
        )
    ).scalar_one() or 0

    age = age_of(user.birth_date) if user.birth_date else "?"
    premium_until = _aware(user.subscription_until)
    muted_until = _aware(user.muted_until)
    last_seen = _aware(user.last_seen_at)

    flags: list[str] = []
    if user.is_banned:
        flags.append(f"🚫 <b>Бан</b> ({escape(user.ban_reason or 'без причины')})")
    if user.is_deleted:
        flags.append("🗑 Удалён")
    if premium_until and premium_until > utcnow():
        flags.append(f"💎 Premium до {premium_until:%d.%m.%Y}")
    if muted_until and muted_until > utcnow():
        flags.append(f"🔇 Мут до {muted_until:%H:%M}")

    lines = [
        f"👤 <b>ID</b> <code>{user.telegram_id}</code> · db#{user.id}",
        f"🚻 {'М' if user.gender == 'male' else 'Ж'} · {age} лет",
        f"📍 {escape(user.city or 'вся страна')}",
        f"🔎 Ищет: {escape(intent or '—')}",
        f"⭐ Рейтинг: <b>{user.season_rating_chat:.2f}</b> "
        f"({user.chat_ratings_count} оценок, {user.last_20_avg_chat:.2f} за 20)",
        f"🖼 Внешность: <b>{user.season_rating_appearance:.2f}</b> "
        f"({user.appearance_ratings_count} оценок)",
        "",
        f"📅 Регистрация: {user.created_at:%d.%m.%Y}" if user.created_at else "📅 —",
        f"👀 Был(а): {last_seen:%d.%m %H:%M} UTC" if last_seen else "👀 —",
        f"💬 Диалогов: {dialogs} · 💸 Оплачено: {float(paid) / 100:.0f} ₽",
        f"🚨 Жалоб: {open_c} открытых / {total_c} всего",
        "",
        (" ".join(flags) if flags else "✅ Чист"),
    ]
    return UserCard(
        user=user,
        text="\n".join(lines),
        open_complaints=open_c,
        total_complaints=total_c,
        dialogs=dialogs,
        messages=0,
        payments_rub=float(paid) / 100,
        intent=intent,
    )


# ------------------------------------------------------------------- user actions
@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    text: str


async def set_ban(
    session: AsyncSession,
    user: User,
    *,
    admin_tg: int,
    banned: bool,
    reason: str = "нарушение правил",
) -> ActionResult:
    user.is_banned = banned
    user.ban_reason = reason if banned else None
    await session.commit()
    await log_action(
        session,
        admin_tg=admin_tg,
        action="ban" if banned else "unban",
        target_tg=int(user.telegram_id),
        details=reason,
    )
    return ActionResult(True, ("Забанен" if banned else "Разбанен") + f": {user.telegram_id}")


async def set_mute(session: AsyncSession, user: User, *, admin_tg: int, hours: int = 1) -> ActionResult:
    user.muted_until = utcnow() + timedelta(hours=hours)
    await session.commit()
    await log_action(
        session, admin_tg=admin_tg, action="mute", target_tg=int(user.telegram_id), details=f"{hours}h"
    )
    return ActionResult(True, f"Мут {hours}ч: {user.telegram_id}")


async def grant_premium(session: AsyncSession, user: User, *, admin_tg: int, days: int = 30) -> ActionResult:
    until = _aware(user.subscription_until)
    base = until if until and until > utcnow() else utcnow()
    user.subscription_until = base + timedelta(days=days)
    await session.commit()
    await log_action(
        session, admin_tg=admin_tg, action="grant_premium", target_tg=int(user.telegram_id), details=f"+{days}d"
    )
    return ActionResult(True, f"Premium +{days}д: {user.telegram_id}")


async def reset_rating(session: AsyncSession, user: User, *, admin_tg: int) -> ActionResult:
    await session.execute(delete(Rating).where(Rating.to_user_id == user.id))
    user.season_rating_chat = 0.0
    user.season_rating_appearance = 0.0
    user.last_20_avg_chat = 0.0
    user.last_20_avg_appearance = 0.0
    user.chat_ratings_count = 0
    user.appearance_ratings_count = 0
    await session.commit()
    await log_action(session, admin_tg=admin_tg, action="reset_rating", target_tg=int(user.telegram_id))
    return ActionResult(True, f"Рейтинг сброшен: {user.telegram_id}")


async def delete_user_data(session: AsyncSession, user: User, *, admin_tg: int) -> ActionResult:
    """Hard delete on a user's request (152-ФЗ ст. 21). Payments stay for accounting."""
    tg = int(user.telegram_id)
    await session.delete(user)
    await session.commit()
    await log_action(session, admin_tg=admin_tg, action="gdpr_delete", target_tg=tg)
    return ActionResult(True, f"Данные удалены: {tg}")


# -------------------------------------------------------------------- complaints
@dataclass(frozen=True, slots=True)
class ComplaintRow:
    id: int
    reason: str
    created_at: datetime
    reporter_tg: int
    accused_tg: int | None
    status: str


async def list_complaints(session: AsyncSession, page: int = 0, per_page: int = 5) -> list[ComplaintRow]:
    res = await session.execute(
        select(
            Complaint.id,
            Complaint.reason,
            Complaint.created_at,
            Complaint.status,
            Complaint.from_user_id,
            Complaint.accused_user_id,
        )
        .where(Complaint.status == ComplaintStatus.open.value)
        .order_by(Complaint.created_at.asc())
        .offset(page * per_page)
        .limit(per_page)
    )
    rows = res.all()
    out: list[ComplaintRow] = []
    for cid, reason, created, status, reporter_id, accused_id in rows:
        reporter = await session.get(User, reporter_id)
        accused = await session.get(User, accused_id) if accused_id else None
        out.append(
            ComplaintRow(
                id=int(cid),
                reason=(reason or "")[:60],
                created_at=created,
                reporter_tg=int(reporter.telegram_id) if reporter else 0,
                accused_tg=int(accused.telegram_id) if accused else None,
                status=str(status.value if hasattr(status, "value") else status),
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class ComplaintDetail:
    complaint: Complaint
    reporter_tg: int
    accused_tg: int | None
    dialog_id: int
    messages: list[tuple[str, str, datetime]]  # (author label, text, created)
    photos: list[str]


async def complaint_detail(
    session: AsyncSession, complaint_id: int, *, limit: int = 15
) -> ComplaintDetail | None:
    complaint = await session.get(Complaint, complaint_id)
    if complaint is None:
        return None
    reporter = await session.get(User, complaint.from_user_id)
    accused = await session.get(User, complaint.accused_user_id) if complaint.accused_user_id else None

    res = await session.execute(
        select(Message.from_user_id, Message.text, Message.created_at)
        .where(Message.dialog_id == complaint.dialog_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    rows = list(res.all())[::-1]
    messages: list[tuple[str, str, datetime]] = []
    for from_user_id, text, created in rows:
        author = "собеседник" if accused and from_user_id == accused.id else "заявитель"
        messages.append((author, (text or "[медиа]")[:400], created))

    photos_res = await session.execute(
        select(Photo.file_path).where(Photo.dialog_id == complaint.dialog_id).limit(3)
    )
    return ComplaintDetail(
        complaint=complaint,
        reporter_tg=int(reporter.telegram_id) if reporter else 0,
        accused_tg=int(accused.telegram_id) if accused else None,
        dialog_id=int(complaint.dialog_id),
        messages=messages,
        photos=[row[0] for row in photos_res.all()],
    )


async def close_complaint(
    session: AsyncSession,
    complaint_id: int,
    *,
    admin_tg: int,
    status: ComplaintStatus,
    resolution: str = "",
) -> ActionResult:
    complaint = await session.get(Complaint, complaint_id)
    if complaint is None:
        return ActionResult(False, "Жалоба не найдена")
    complaint.status = status.value if hasattr(status, "value") else status
    complaint.handled_by = int(admin_tg)
    complaint.handled_at = utcnow()
    complaint.resolution = resolution or None
    await session.commit()
    await log_action(
        session,
        admin_tg=admin_tg,
        action=f"complaint_{status.value}",
        details=f"#{complaint_id} {resolution}"[:2000],
    )
    return ActionResult(True, f"Жалоба #{complaint_id}: {status.value}")


# ----------------------------------------------------------------------- lists
async def user_dialogs(
    session: AsyncSession, user: User, limit: int = 10, page: int = 0
) -> list[Dialog]:
    res = await session.execute(
        select(Dialog)
        .where((Dialog.user1_id == user.id) | (Dialog.user2_id == user.id))
        .order_by(Dialog.created_at.desc())
        .offset(max(0, page) * limit)
        .limit(limit)
    )
    return list(res.scalars().all())


async def dialog_messages(session: AsyncSession, dialog_id: int, limit: int = 40) -> list[str]:
    res = await session.execute(
        select(Message.from_user_id, Message.text, Message.created_at)
        .where(Message.dialog_id == dialog_id)
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    lines: list[str] = []
    for from_user_id, text, created in res.all():
        body = escape((text or "[медиа]")[:300])
        lines.append(f"<i>{created:%H:%M}</i> u{from_user_id}: {body}")
    return lines


async def recent_payments(session: AsyncSession, limit: int = 10) -> list[Payment]:
    res = await session.execute(select(Payment).order_by(Payment.created_at.desc()).limit(limit))
    return list(res.scalars().all())


async def recent_audit(session: AsyncSession, limit: int = 15) -> list[AdminAudit]:
    res = await session.execute(select(AdminAudit).order_by(AdminAudit.created_at.desc()).limit(limit))
    return list(res.scalars().all())


# ------------------------------------------------------------------ maintenance
@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    deleted_messages: int
    deleted_photos: int
    deleted_complaints: int
    deleted_audit: int


async def run_maintenance(session: AsyncSession, settings: Settings) -> MaintenanceReport:
    """Retention enforcement: the privacy policy promises these deletions, so they must run."""
    now = utcnow()
    msg_cutoff = now - timedelta(days=int(settings.retention_message_days))
    complaint_cutoff = now - timedelta(days=int(settings.retention_complaint_days))
    audit_cutoff = now - timedelta(days=int(settings.retention_audit_days))

    photos = await session.execute(delete(Photo).where(Photo.created_at < msg_cutoff))
    messages = await session.execute(delete(Message).where(Message.created_at < msg_cutoff))
    complaints = await session.execute(delete(Complaint).where(Complaint.created_at < complaint_cutoff))
    audit = await session.execute(delete(AdminAudit).where(AdminAudit.created_at < audit_cutoff))
    await session.commit()

    return MaintenanceReport(
        deleted_messages=int(messages.rowcount or 0),
        deleted_photos=int(photos.rowcount or 0),
        deleted_complaints=int(complaints.rowcount or 0),
        deleted_audit=int(audit.rowcount or 0),
    )


__all__ = [
    "ActionResult",
    "ComplaintDetail",
    "ComplaintRow",
    "MaintenanceReport",
    "Stats",
    "UserCard",
    "age_of",
    "close_complaint",
    "complaint_detail",
    "delete_user_data",
    "dialog_messages",
    "find_user",
    "grant_premium",
    "list_complaints",
    "log_action",
    "recent_audit",
    "recent_payments",
    "reset_rating",
    "run_maintenance",
    "set_ban",
    "set_mute",
    "stats",
    "stats_text",
    "user_card",
    "user_dialogs",
    "utcnow",
]

# Keep the unused-import linters honest about the intentional re-exports above.
_ = IntegrityError
