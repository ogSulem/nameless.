from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiohttp import web

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthServer:
    runner: web.AppRunner
    site: web.TCPSite
    started: asyncio.Event


async def start_health_server(*, host: str, port: int) -> HealthServer:
    started = asyncio.Event()

    async def healthz(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def readyz(_request: web.Request) -> web.Response:
        if started.is_set():
            return web.Response(text="ready")
        return web.Response(status=503, text="starting")

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    site = web.TCPSite(runner, host=host, port=int(port))
    await site.start()
    started.set()

    logger.info("health_server_started host=%s port=%s", host, port)
    return HealthServer(runner=runner, site=site, started=started)


async def stop_health_server(server: HealthServer | None) -> None:
    if server is None:
        return
    try:
        await server.runner.cleanup()
    except Exception:
        logger.exception("health_server_stop_failed")
