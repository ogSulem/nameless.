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
        """Both collusion checks in one round trip.

        They used to be two or three separate COUNTs; as scalar subqueries they
        cost one statement and see the same snapshot.
        """
        since = datetime.now(tz=UTC) - timedelta(days=7)
        pair = (from_user_id, to_user_id)

        def _ratings(*extra):
            return (
                select(func.count(Rating.id))
                .where(Rating.created_at >= since)
                .where(Rating.rating_type == RatingType.chat)
                .where(and_(Rating.from_user_id.in_(pair), Rating.to_user_id.in_(pair)))
                .where(*extra)
                .scalar_subquery()
            )

        met = (
            select(func.count(Dialog.id))
            .where(Dialog.created_at >= since)
            .where(and_(Dialog.user1_id.in_(pair), Dialog.user2_id.in_(pair)))
            .scalar_subquery()
        )
        mutual, high = _ratings(), _ratings(Rating.value >= 9)
        met_count, mutual_count, high_count = (
            int(v or 0) for v in (await session.execute(select(met, mutual, high))).one()
        )

        if met_count > 3:
            return AntiAbuseDecision(False, "pair_met_too_often")
        if mutual_count >= 10 and high_count / mutual_count >= 0.8:
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

    async def recalculate(
        self, session: AsyncSession, to_user_id: int | User, *, commit: bool = True
    ) -> RatingUpdate:
        user = to_user_id if isinstance(to_user_id, User) else await session.get(User, to_user_id)
        if user is None:
            return RatingUpdate(False, 0.0, 0.0, 0.0, 0, 0, False)

        previous_chat = float(user.season_rating_chat or 0.0)
        window = max(1, int(self._settings.rating_window))

        chat_window, chat_total, app_window, app_total = await _windows(session, int(user.id), window)

        user.chat_ratings_count = chat_total
        user.appearance_ratings_count = app_total

        user.season_rating_chat = self.blended(chat_window)
        user.season_rating_appearance = self.blended(app_window)
        user.last_20_avg_chat = _mean(chat_window[-20:])
        user.last_20_avg_appearance = _mean(app_window[-20:])

        if chat_total >= int(self._settings.rating_min_count) and previous_chat:
            if previous_chat - user.season_rating_chat >= 3:
                user.is_under_review = True

        if commit:
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("rating_recalculate_commit_failed user_id=%s", user.id)
                raise

        logger.info(
            "rating_recalculated user_id=%s chat=%s (n=%s) appearance=%s (n=%s)",
            user.id, user.season_rating_chat, user.chat_ratings_count,
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


async def _windows(
    session: AsyncSession, to_user_id: int, window: int
) -> tuple[list[int], int, list[int], int]:
    """Both rating windows *and* both totals in a single statement.

    Only the newest `window` rows feed the maths (a long-lived user's full
    history does not scale), but the totals still need every row, so they come
    back as a window function over the same scan.
    """
    rn = func.row_number().over(
        partition_by=Rating.rating_type,
        order_by=(Rating.created_at.desc(), Rating.id.desc()),
    )
    total = func.count().over(partition_by=Rating.rating_type)
    inner = (
        select(Rating.rating_type, Rating.value, rn.label("rn"), total.label("total"))
        .where(Rating.to_user_id == to_user_id, Rating.is_seasonal_valid.is_(True))
        .subquery()
    )
    rows = (
        await session.execute(
            select(inner.c.rating_type, inner.c.value, inner.c.total)
            .where(inner.c.rn <= max(1, int(window)))
            .order_by(inner.c.rating_type, inner.c.rn.desc())
        )
    ).all()

    chat: list[int] = []
    app: list[int] = []
    chat_total = app_total = 0
    for rating_type, value, count in rows:
        kind = rating_type.value if hasattr(rating_type, "value") else rating_type
        if kind == RatingType.chat.value:
            chat.append(int(value))
            chat_total = int(count)
        else:
            app.append(int(value))
            app_total = int(count)
    return chat, chat_total, app, app_total


def _mean(values: list[int]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0
