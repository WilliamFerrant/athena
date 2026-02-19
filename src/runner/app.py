"""Runner FastAPI application — auth middleware + project registry."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from src.projects.registry import ProjectRegistry
from src.runner.config import runner_settings
from src.runner.endpoints import router

logger = logging.getLogger(__name__)


# ── Auth middleware ───────────────────────────────────────────────────────────


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests missing or having an invalid X-Runner-Token header."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip auth if no token is configured (dev mode)
        token = runner_settings.runner_token
        if not token:
            return await call_next(request)

        provided = request.headers.get("X-Runner-Token", "")
        if provided != token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing X-Runner-Token"},
            )

        return await call_next(request)


# ── Lifespan ─────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load project registry on startup."""
    projects_path = Path(runner_settings.runner_projects_file)
    if not projects_path.is_absolute():
        projects_path = Path.cwd() / projects_path

    registry = ProjectRegistry(path=projects_path)
    try:
        registry.load()
        logger.info(
            "Runner registry loaded: %d projects from %s",
            len(registry._projects),
            projects_path,
        )
    except Exception:
        logger.warning("Failed to load projects file: %s", projects_path)

    app.state.registry = registry

    yield


# ── App factory ──────────────────────────────────────────────────────────────


def create_runner_app() -> FastAPI:
    """Create the runner FastAPI application."""
    app = FastAPI(
        title="CLA Runner — Local Command Executor",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(TokenAuthMiddleware)
    app.include_router(router)

    return app


runner_app = create_runner_app()
