from __future__ import annotations

from aiogram import Router

from app.handlers.admin import router as admin_router
from app.handlers.commands import router as commands_router
from app.handlers.dialog import router as dialog_router
from app.handlers.fallback import router as fallback_router
from app.handlers.promo import router as promo_router
from app.handlers.rating import router as rating_router
from app.handlers.search import router as search_router
from app.handlers.subscription import router as subscription_router


def build_router() -> Router:
    """Order matters.

    * commands/registration first (state-scoped handlers),
    * admin & payments before the dialog relay (state-scoped text inputs),
    * dialog relay is a catch-all: everything below it would never see a message,
    * fallback last.
    """
    root = Router(name="root")
    root.include_router(commands_router)
    root.include_router(admin_router)
    root.include_router(promo_router)
    root.include_router(search_router)
    root.include_router(rating_router)
    root.include_router(subscription_router)
    root.include_router(dialog_router)
    root.include_router(fallback_router)
    return root
