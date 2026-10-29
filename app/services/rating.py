from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Dialog, Rating, RatingType, User

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AntiAbuseDecision:
    is_seasonal_valid: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class RatingUpdate:
    ok: bool
    chat_rating: float
    appearance_rating: float
    previous_chat_rating: float
    chat_count: int
    appearance_count: int
    seasonal_valid: bool


class RatingService:
    """Rating maths.

    Design decisions (both were missing before and made ratings meaningless):

    * **Bayesian smoothing.** A single 0 would permanently pin a new user at
      "0.0". With a prior of 5.0 weighted 5 votes, one vote moves the average by
      a realistic amount and converges to the true mean over time.
    * **Anti-abuse flags are actually applied.** `is_seasonal_valid == False`
      rows are excluded from the aggregate, so the collusion detectors do
      something instead of just writing a boolean.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ------------------------------------------------------------------ abuse
    async def decide_seasonal_validity(
        self,
        session: AsyncSession,
        dialog_id: int,
        from_user_id: int,
        to_user_id: int,
    ) -> AntiAbuseDecision:
        since = datetime.now(tz=UTC) - timedelta(days=7)
        pair = (from_user_id, to_user_id)

        meets = int(
            (
                await session.execute(
                    select(func.count(Dialog.id))
                    .where(Dialog.created_at >= since)
                    .where(and_(Dialog.user1_id.in_(pair), Dialog.user2_id.in_(pair)))
                )
            ).scalar_one()
        )
        if meets > 3:
            return AntiAbuseDecision(False, "pair_met_too_often")

        mutual = int(
            (
                await session.execute(
                    select(func.count(Rating.id))
                    .where(Rating.created_at >= since)
                    .where(Rating.rating_type == RatingType.chat)
                    .where(and_(Rating.from_user_id.in_(pair), Rating.to_user_id.in_(pair)))
                )
            ).scalar_one()
        )
        if mutual >= 10:
            high = int(
                (
                    await session.execute(
                        select(func.count(Rating.id))
                        .where(Rating.created_at >= since)
                        .where(Rating.rating_type == RatingType.chat)
                        .where(Rating.value >= 9)
                        .where(and_(Rating.from_user_id.in_(pair), Rating.to_user_id.in_(pair)))
                    )
                ).scalar_one()
            )
            if high / mutual >= 0.8:
                return AntiAbuseDecision(False, "mutual_high_ratio")

        return AntiAbuseDecision(True, None)

    # ------------------------------------------------------------ aggregation
    def blended(self, values: list[int]) -> float:
        """Bayesian average: pulls small samples towards the prior mean."""
        if not values:
            return 0.0
        prior_mean = float(self._settings.rating_prior_mean)
        prior_weight = float(self._settings.rating_prior_weight)
        total = float(sum(values)) + prior_mean * prior_weight
        return round(total / (len(values) + prior_weight), 2)

    async def recalculate(self, session: AsyncSession, to_user_id: int) -> RatingUpdate:
        user = await session.get(User, to_user_id)
        if user is None:
            return RatingUpdate(False, 0.0, 0.0, 0.0, 0, 0, False)

        previous_chat = float(user.season_rating_chat or 0.0)
        window = max(1, int(self._settings.rating_window))

        res = await session.execute(
            select(Rating.rating_type, Rating.value, Rating.created_at)
            .where(Rating.to_user_id == to_user_id)
            .where(Rating.is_seasonal_valid.is_(True))
            .order_by(Rating.created_at.asc())
        )
        chat_values: list[int] = []
        app_values: list[int] = []
        for rating_type, value, _created in res.all():
            if rating_type == RatingType.chat:
                chat_values.append(int(value))
            else:
                app_values.append(int(value))

        chat_window = chat_values[-window:]
        app_window = app_values[-window:]

        user.chat_ratings_count = len(chat_values)
        user.appearance_ratings_count = len(app_values)
        user.calibration_counter = len(chat_values)

        user.season_rating_chat = self.blended(chat_window)
        user.season_rating_appearance = self.blended(app_window)
        user.last_20_avg_chat = _mean(chat_values[-20:])
        user.last_20_avg_appearance = _mean(app_values[-20:])

        if len(chat_values) >= int(self._settings.rating_min_count) and previous_chat:
            if previous_chat - user.season_rating_chat >= 3:
                user.is_under_review = True

        try:
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("rating_recalculate_commit_failed user_id=%s", to_user_id)
            raise

        logger.info(
            "rating_recalculated user_id=%s chat=%s (n=%s) appearance=%s (n=%s)",
            to_user_id, user.season_rating_chat, user.chat_ratings_count,
            user.season_rating_appearance, user.appearance_ratings_count,
        )
        return RatingUpdate(
            ok=True,
            chat_rating=float(user.season_rating_chat),
            appearance_rating=float(user.season_rating_appearance),
            previous_chat_rating=previous_chat,
            chat_count=int(user.chat_ratings_count),
            appearance_count=int(user.appearance_ratings_count),
            seasonal_valid=True,
        )


def _mean(values: list[int]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0
