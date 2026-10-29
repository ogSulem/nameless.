from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import web

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HealthServer:
    runner: web.AppRunner
    site: web.TCPSite


async def start_health_server(
    *,
    host: str,
    port: int,
    check_db=None,
    check_redis=None,
    routes: list[tuple[str, str, Callable[[web.Request], Awaitable[web.StreamResponse]]]] | None = None,
) -> HealthServer:
    """Liveness + readiness with real dependency checks.

    `/healthz` stays dumb (process is up), `/readyz` fails when Postgres or Redis
    are unreachable — the previous version reported "ready" no matter what, so a
    dead database looked perfectly healthy to the platform.
    """

    async def healthz(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def readyz(_request: web.Request) -> web.Response:
        problems: list[str] = []
        if check_db is not None:
            try:
                await asyncio.wait_for(check_db(), timeout=3)
            except Exception as exc:
                problems.append(f"db:{type(exc).__name__}")
        if check_redis is not None:
            try:
                await asyncio.wait_for(check_redis(), timeout=3)
            except Exception as exc:
                problems.append(f"redis:{type(exc).__name__}")
        if problems:
            return web.Response(status=503, text="not ready: " + ",".join(problems))
        return web.Response(text="ready")

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    for method, path, handler in routes or []:
        app.router.add_route(method, path, handler)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=int(port))
    await site.start()
    logger.info("health_server_started host=%s port=%s", host, port)
    return HealthServer(runner=runner, site=site)


async def stop_health_server(server: HealthServer | None) -> None:
    if server is None:
        return
    try:
        await server.runner.cleanup()
    except Exception:
        logger.warning("health_server_stop_failed", exc_info=True)
