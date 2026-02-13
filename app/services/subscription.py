from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User

logger = logging.getLogger(__name__)


class SubscriptionService:
    async def extend_subscription(self, session: AsyncSession, telegram_id: int, days: int) -> datetime | None:
        res = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = res.scalar_one_or_none()
        if user is None:
            return None

        now = datetime.now(tz=timezone.utc)
        base = user.subscription_until
        if base is None:
            base_dt = now
        else:
            base_dt = base if base.tzinfo else base.replace(tzinfo=timezone.utc)
            if base_dt < now:
                base_dt = now

        user.subscription_until = base_dt + timedelta(days=days)
        try:
            await session.commit()
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            raise
        logger.info("subscription_extended tg_id=%s until=%s", telegram_id, user.subscription_until)
        return user.subscription_until
