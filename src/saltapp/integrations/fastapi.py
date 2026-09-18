# Thin FastAPI/Starlette adapter for saltapp.agent.Agent. Only needed if you
# already have a FastAPI app and want to mount Salt's webhook route inside
# it -- if you're starting fresh, `Agent.asgi_app()` needs no framework at
# all and is the simpler path (see the README's webhook example).
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from saltapp.agent import Agent

try:
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "saltapp.integrations.fastapi requires the 'fastapi' extra: pip install 'saltapp[fastapi]'"
    ) from exc

from saltapp.webhook import WebhookVerificationError, handle

_logger = logging.getLogger("saltapp.integrations.fastapi")


def create_router(agent: "Agent", *, prefix: str = "") -> APIRouter:
    """An APIRouter with `POST {prefix}/` (the webhook) and `GET
    {prefix}/health`. Mount it on your own FastAPI app:

        app = FastAPI()
        app.include_router(saltapp.integrations.fastapi.create_router(agent))
    """
    router = APIRouter(prefix=prefix)

    @router.post("/")
    async def webhook(request: Request) -> JSONResponse:
        raw = await request.body()
        try:
            event = handle(
                request.headers,
                raw,
                secret=agent.webhook_secret,
                verify=agent.verify_signatures,
                tolerance_seconds=agent.signature_tolerance_seconds,
            )
        except WebhookVerificationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)

        async def _run() -> None:
            try:
                await agent.dispatch(event)
            except Exception as exc:  # noqa: BLE001
                _logger.error("[webhook] dispatch failed: %s", exc)

        # Ack immediately, dispatch in the background -- a slow agent-loop
        # call must never make salt-api's webhook delivery time out.
        asyncio.create_task(_run())
        return JSONResponse({"status": "accepted"})

    @router.get("/health")
    async def health() -> dict:
        return {"status": "ok", **agent.health_extra()}

    return router


def create_app(agent: "Agent"):
    """A standalone FastAPI app, if you don't have one already."""
    from fastapi import FastAPI

    app = FastAPI(title="saltapp agent webhook")
    app.include_router(create_router(agent))
    return app
