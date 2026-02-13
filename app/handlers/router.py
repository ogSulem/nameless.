from __future__ import annotations

from aiogram import Router

from app.handlers.start import router as start_router
from app.handlers.search import router as search_router
from app.handlers.dialog import router as dialog_router
from app.handlers.rating import router as rating_router
from app.handlers.subscription import router as subscription_router
from app.handlers.admin_dump import router as admin_dump_router
from app.handlers.cleanup import router as cleanup_router


def build_router() -> Router:
    router = Router(name="root")
    router.include_router(start_router)
    router.include_router(search_router)
    router.include_router(dialog_router)
    router.include_router(rating_router)
    router.include_router(subscription_router)
    router.include_router(admin_dump_router)
    router.include_router(cleanup_router)
    return router
