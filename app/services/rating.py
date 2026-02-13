from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Dialog, Rating, RatingType, User

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AntiAbuseDecision:
    is_seasonal_valid: bool
    reason: str | None


class RatingService:
    async def decide_seasonal_validity(
        self,
        session: AsyncSession,
        dialog_id: int,
        from_user_id: int,
        to_user_id: int,
    ) -> AntiAbuseDecision:
        now = datetime.now(tz=timezone.utc)
        since = now - timedelta(days=7)

        meets_q: Select = (
            select(func.count(Dialog.id))
            .where(Dialog.created_at >= since)
            .where(
                and_(
                    Dialog.user1_id.in_([from_user_id, to_user_id]),
                    Dialog.user2_id.in_([from_user_id, to_user_id]),
                )
            )
        )
        meets = int((await session.execute(meets_q)).scalar_one())
        if meets > 3:
            return AntiAbuseDecision(is_seasonal_valid=False, reason="pair_met_too_often")

        mutual_high_q: Select = (
            select(func.count(Rating.id))
            .where(Rating.created_at >= since)
            .where(Rating.rating_type == RatingType.chat)
            .where(
                and_(
                    Rating.from_user_id.in_([from_user_id, to_user_id]),
                    Rating.to_user_id.in_([from_user_id, to_user_id]),
                )
            )
        )
        mutual_total = int((await session.execute(mutual_high_q)).scalar_one())
        if mutual_total >= 10:
            mutual_high_q2: Select = (
                select(func.count(Rating.id))
                .where(Rating.created_at >= since)
                .where(Rating.rating_type == RatingType.chat)
                .where(Rating.value >= 9)
                .where(
                    and_(
                        Rating.from_user_id.in_([from_user_id, to_user_id]),
                        Rating.to_user_id.in_([from_user_id, to_user_id]),
                    )
                )
            )
            mutual_high = int((await session.execute(mutual_high_q2)).scalar_one())
            if mutual_high / mutual_total >= 0.8:
                return AntiAbuseDecision(is_seasonal_valid=False, reason="mutual_high_ratio")

        return AntiAbuseDecision(is_seasonal_valid=True, reason=None)

    async def on_rating_saved(self, session: AsyncSession, to_user_id: int) -> tuple[bool, float, float, float]:
        received_dialogs_q = (
            select(func.count(func.distinct(Rating.dialog_id)))
            .where(Rating.to_user_id == to_user_id)
            .where(Rating.rating_type == RatingType.chat)
        )
        total_received_dialogs = int((await session.execute(received_dialogs_q)).scalar_one())

        user = await session.get(User, to_user_id)
        if user is None:
            return (False, 0.0, 0.0, 0.0)

        prev_chat = float(user.season_rating_chat or 0.0)

        user.calibration_counter = total_received_dialogs

        avg_chat_q = (
            select(func.avg(Rating.value))
            .where(Rating.to_user_id == to_user_id)
            .where(Rating.rating_type == RatingType.chat)
        )
        avg_app_q = (
            select(func.avg(Rating.value))
            .where(Rating.to_user_id == to_user_id)
            .where(Rating.rating_type == RatingType.appearance)
        )

        avg_chat = float((await session.execute(avg_chat_q)).scalar_one() or 0.0)
        avg_app = float((await session.execute(avg_app_q)).scalar_one() or 0.0)

        last20_chat_subq = (
            select(Rating.value)
            .where(Rating.to_user_id == to_user_id)
            .where(Rating.rating_type == RatingType.chat)
            .order_by(Rating.created_at.desc())
            .limit(20)
        ).subquery()
        last20_chat = float((await session.execute(select(func.avg(last20_chat_subq.c.value)))).scalar_one() or 0.0)

        last20_app_subq = (
            select(Rating.value)
            .where(Rating.to_user_id == to_user_id)
            .where(Rating.rating_type == RatingType.appearance)
            .order_by(Rating.created_at.desc())
            .limit(20)
        ).subquery()
        last20_app = float((await session.execute(select(func.avg(last20_app_subq.c.value)))).scalar_one() or 0.0)

        user.last_20_avg_chat = last20_chat
        user.last_20_avg_appearance = last20_app

        user.season_rating_chat = avg_chat
        user.season_rating_appearance = avg_app

        if prev_chat and (prev_chat - avg_chat) >= 3:
            user.is_under_review = True

        try:
            await session.commit()
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            raise
        logger.info("rating_recalculated user_id=%s avg_chat=%s avg_app=%s", to_user_id, avg_chat, avg_app)
        return (True, avg_chat, avg_app, prev_chat)
