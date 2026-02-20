"""FastAPI server for the multi-agent system."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.agents.backend import BackendAgent
from src.agents.frontend import FrontendAgent
from src.agents.manager import ManagerAgent
from src.agents.tester import TesterAgent
from src.api.health_routes import broadcast_result, health_router
from src.api.routes import router
from src.api.runner_routes import runner_router
from src.api.task_routes import task_router
from src.config import settings
from src.health.engine import HealthStore
from src.health.scheduler import HealthScheduler
from src.memory.mem0_client import AgentMemory
from src.projects.registry import ProjectRegistry
from src.runner_connector.client import RunnerClient
from src.runner_connector.poller import RunnerPoller
from src.tasks.store import TaskStore
from src.token_tracker.tracker import TokenTracker

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared resources on startup."""
    # Token tracker
    tracker = TokenTracker()
    app.state.tracker = tracker

    # Shared agent pool — persistent across requests so conversation history
    # and memory context accumulate naturally within a server session.
    def _make_memory(agent_id: str) -> AgentMemory | None:
        try:
            return AgentMemory(agent_id=agent_id)
        except Exception:
            logger.warning("mem0 unavailable for %s — running without memory", agent_id)
            return None

    app.state.agents = {
        "manager": ManagerAgent(
            agent_id="manager", tracker=tracker, memory=_make_memory("manager")
        ),
        "frontend": FrontendAgent(
            agent_id="frontend", tracker=tracker, memory=_make_memory("frontend")
        ),
        "backend": BackendAgent(
            agent_id="backend", tracker=tracker, memory=_make_memory("backend")
        ),
        "tester": TesterAgent(
            agent_id="tester", tracker=tracker, memory=_make_memory("tester")
        ),
    }
    logger.info("Agent pool initialised with memory=%s", "mem0" if app.state.agents["manager"].memory else "none")

    # Project registry
    registry = ProjectRegistry()
    try:
        registry.load()
        logger.info("Project registry loaded: %d projects", len(registry._projects))
    except Exception:
        logger.warning("Failed to load projects.yaml — health checks disabled")
    app.state.registry = registry

    # Health engine
    store = HealthStore()
    app.state.health_store = store

    # Health scheduler
    scheduler = HealthScheduler(registry, store, on_result=broadcast_result)
    app.state.health_scheduler = scheduler

    try:
        await scheduler.start()
    except Exception:
        logger.exception("Health scheduler failed to start")

    # Runner connector
    runner_client = RunnerClient(
        base_url=settings.runner_base_url,
        token=settings.runner_token,
    )
    app.state.runner_client = runner_client

    # Task board
    task_store = TaskStore()
    app.state.task_store = task_store

    runner_poller = RunnerPoller(
        client=runner_client,
        interval=float(settings.runner_poll_interval),
    )
    app.state.runner_poller = runner_poller

    try:
        await runner_poller.start()
        logger.info(
            "Runner poller started — base_url=%s interval=%ds",
            settings.runner_base_url,
            settings.runner_poll_interval,
        )
    except Exception:
        logger.exception("Runner poller failed to start")

    yield

    # Shutdown
    await runner_poller.stop()
    await scheduler.stop()
    store.close()
    task_store.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Athena - Multi-Agent System",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix="/api")
    app.include_router(health_router, prefix="/api")
    app.include_router(runner_router, prefix="/api")
    app.include_router(task_router, prefix="/api")

    # Serve unified dashboard at root and /workshop (same SPA, mode toggled client-side)
    @app.get("/")
    async def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/workshop")
    async def workshop():
        return FileResponse(STATIC_DIR / "index.html")

    # Serve other static assets
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


app = create_app()

