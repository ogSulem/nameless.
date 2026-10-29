"""Promo codes: free Premium without lying to anyone.

Design rules that keep this from becoming a money leak:

* every code has a hard usage cap and a deadline — no code lives forever;
* one redemption per person per code, enforced by a unique index, not by hope;
* the counter is incremented atomically with ``used_count < max_uses`` in the
  WHERE clause, so two parallel redemptions of the last slot cannot both win;
* codes are validated before the user is charged anything (they never pay), and
  every redemption ends up in the admin audit log.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PromoCode, PromoRedemption, User
from app.services.subscription import SubscriptionService

logger = logging.getLogger(__name__)

CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,23}$")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, no 1/I
MAX_DAYS = 365
MAX_USES = 100_000


class RedeemStatus(str, Enum):
    ok = "ok"
    bad_format = "bad_format"
    not_found = "not_found"
    inactive = "inactive"
    expired = "expired"
    exhausted = "exhausted"
    already_used = "already_used"
    banned = "banned"
    not_registered = "not_registered"


@dataclass(frozen=True, slots=True)
class RedeemResult:
    status: RedeemStatus
    code: str = ""
    days: int = 0
    until: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status is RedeemStatus.ok


@dataclass(frozen=True, slots=True)
class PromoRow:
    id: int
    code: str
    days: int
    used_count: int
    max_uses: int
    is_active: bool
    expires_at: datetime | None
    note: str | None


def normalize(raw: str | None) -> str:
    """Users type codes with spaces, dashes and lowercase. Accept all of that."""
    text = (raw or "").strip().upper()
    text = re.sub(r"\s+", "", text)
    return text[:32]


def generate_code(prefix: str = "PROMO", length: int = 6) -> str:
    tail = "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
    return f"{prefix.upper()}-{tail}"


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def create_code(
    session: AsyncSession,
    *,
    code: str,
    days: int = 30,
    max_uses: int = 1,
    valid_days: int | None = 30,
    note: str | None = None,
    created_by: int | None = None,
) -> tuple[PromoCode | None, str | None]:
    """Create a code. Returns (code, error). Error is human-readable Russian."""
    code = normalize(code)
    if not CODE_RE.match(code):
        return None, "Код должен быть 3–24 символа: латиница, цифры, дефис или подчёркивание."

    if not 1 <= days <= MAX_DAYS:
        return None, f"Срок Premium должен быть от 1 до {MAX_DAYS} дней."
    if not 1 <= max_uses <= MAX_USES:
        return None, f"Число активаций должно быть от 1 до {MAX_USES}."
    if valid_days is not None and not 1 <= valid_days <= MAX_DAYS:
        return None, f"Срок жизни кода должен быть от 1 до {MAX_DAYS} дней."

    existing = (
        await session.execute(select(PromoCode.id).where(PromoCode.code == code))
    ).scalar_one_or_none()
    if existing is not None:
        return None, "Такой код уже есть. Возьмите другой."

    expires_at = _now() + timedelta(days=valid_days) if valid_days else None
    promo = PromoCode(
        code=code,
        days=days,
        max_uses=max_uses,
        used_count=0,
        is_active=True,
        note=(note or None),
        created_by=created_by,
        expires_at=expires_at,
    )
    session.add(promo)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None, "Такой код уже есть. Возьмите другой."
    logger.info("promo_created code=%s days=%s uses=%s", code, days, max_uses)
    return promo, None


async def redeem(session: AsyncSession, *, telegram_id: int, raw_code: str) -> RedeemResult:
    code = normalize(raw_code)
    if not CODE_RE.match(code):
        return RedeemResult(RedeemStatus.bad_format, code=code)

    promo = (
        await session.execute(select(PromoCode).where(PromoCode.code == code))
    ).scalar_one_or_none()
    if promo is None:
        return RedeemResult(RedeemStatus.not_found, code=code)
    if not promo.is_active:
        return RedeemResult(RedeemStatus.inactive, code=code)
    expires_at = _aware(promo.expires_at)
    if expires_at is not None and expires_at <= _now():
        return RedeemResult(RedeemStatus.expired, code=code)
    if promo.used_count >= promo.max_uses:
        return RedeemResult(RedeemStatus.exhausted, code=code)

    user = (
        await session.execute(select(User).where(User.telegram_id == int(telegram_id)))
    ).scalar_one_or_none()
    if user is None or user.is_deleted:
        return RedeemResult(RedeemStatus.not_registered, code=code)
    if user.is_banned or user.is_under_review:
        return RedeemResult(RedeemStatus.banned, code=code)

    already = (
        await session.execute(
            select(PromoRedemption.id).where(
                PromoRedemption.code_id == promo.id,
                PromoRedemption.telegram_id == int(telegram_id),
            )
        )
    ).scalar_one_or_none()
    if already is not None:
        return RedeemResult(RedeemStatus.already_used, code=code, days=promo.days)

    # Atomic claim: the last slot can only go to one person.
    claimed = await session.execute(
        update(PromoCode)
        .where(
            PromoCode.id == promo.id,
            PromoCode.is_active.is_(True),
            PromoCode.used_count < PromoCode.max_uses,
        )
        .values(used_count=PromoCode.used_count + 1)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        return RedeemResult(RedeemStatus.exhausted, code=code)

    session.add(
        PromoRedemption(code_id=promo.id, telegram_id=int(telegram_id), days=promo.days)
    )
    try:
        await session.flush()
    except IntegrityError:
        # Lost a race against the same person's parallel tap: undo the counter.
        await session.rollback()
        return RedeemResult(RedeemStatus.already_used, code=code, days=promo.days)

    until = await SubscriptionService().extend_subscription(
        session=session, telegram_id=int(telegram_id), days=promo.days
    )
    logger.info("promo_redeemed code=%s tg=%s days=%s", code, telegram_id, promo.days)
    return RedeemResult(RedeemStatus.ok, code=code, days=promo.days, until=until)


async def list_codes(session: AsyncSession, limit: int = 10) -> list[PromoRow]:
    rows = (
        await session.execute(
            select(PromoCode).order_by(PromoCode.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    return [
        PromoRow(
            id=p.id,
            code=p.code,
            days=p.days,
            used_count=p.used_count,
            max_uses=p.max_uses,
            is_active=p.is_active,
            expires_at=_aware(p.expires_at),
            note=p.note,
        )
        for p in rows
    ]


async def toggle_active(session: AsyncSession, code_id: int) -> PromoCode | None:
    promo = await session.get(PromoCode, code_id)
    if promo is None:
        return None
    promo.is_active = not promo.is_active
    await session.commit()
    return promo


async def set_active(session: AsyncSession, code_id: int, active: bool) -> PromoCode | None:
    promo = await session.get(PromoCode, code_id)
    if promo is None:
        return None
    promo.is_active = active
    await session.commit()
    return promo


async def redemption_count(session: AsyncSession) -> int:
    from sqlalchemy import func

    return int(
        (await session.execute(select(func.count(PromoRedemption.id)))).scalar_one() or 0
    )
