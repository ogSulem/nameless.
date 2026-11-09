"""Account erasure and moderation side effects.

One place that knows what "delete this person" means, so the self-service button
and the admin action can never drift apart. Photos and texts live in
`messages`/`photos` and disappear with the user row (ON DELETE CASCADE); the
Redis cache is dropped explicitly because it is not part of the database.
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import ActiveDialog, User
from app.redis import keys
from app.services.dialog import DialogService

logger = logging.getLogger(__name__)


async def purge_user(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    tg_id: int,
) -> list[int]:
    """Erase a profile; returns the telegram ids of partners whose dialog was closed."""
    partners: list[int] = []

    dialog_id = await DialogService(redis, settings).active_dialog_id(session, int(tg_id))
    if dialog_id:
        participants = await DialogService(redis, settings).finish(
            session, dialog_id, actor_tg_id=int(tg_id)
        )
        partners = [tg for tg in (participants or []) if int(tg) != int(tg_id)]

    user = (
        await session.execute(select(User).where(User.telegram_id == int(tg_id)))
    ).scalar_one_or_none()
    if user is not None:
        await session.execute(delete(ActiveDialog).where(ActiveDialog.user_id == user.id))
        await session.delete(user)  # cascades to dialogs / messages / photos / ratings
        await session.commit()

    await clear_user_cache(redis, tg_id)
    logger.info("user_purged tg=%s partners=%s", tg_id, partners)
    return partners


async def clear_user_cache(redis: Redis, tg_id: int) -> None:
    """Nothing about a deleted user may survive in Redis."""
    try:
        pipe = redis.pipeline(transaction=False)
        for key in (
            keys.ui_message_id(tg_id),
            keys.ui_search_message_id(tg_id),
            keys.active_dialog(tg_id),
            keys.pending_rating(tg_id),
            keys.pending_rating_partner(tg_id),
            keys.pending_rating_step(tg_id),
            keys.pending_rating_has_photos(tg_id),
            keys.pending_rating_action(tg_id),
            keys.queue_member_key(tg_id),
            keys.seek_gender_key(tg_id),
            keys.seek_display_key(tg_id),
            keys.premium_pref_key(tg_id),
            keys.last_partner(tg_id),
            keys.online_key(tg_id),
        ):
            pipe.delete(key)
        await pipe.execute()
    except Exception:
        logger.warning("purge_cache_failed tg=%s", tg_id, exc_info=True)
