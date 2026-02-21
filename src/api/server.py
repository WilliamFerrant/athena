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
from src.api.diag_routes import diag_router
from src.autonomy.heartbeat import HeartbeatScheduler
from src.config import settings
from src.health.engine import HealthStore
from src.health.scheduler import HealthScheduler
from src.memory.curator import MemoryCurator, MemoryCurationStore
from src.memory.mem0_client import AgentMemory
from src.notifications.discord import DiscordNotifier, DiscordBotPoller
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

    # MCP living context interceptor
    try:
        from src.context import init_mcp
        from src.memory.graph_context import get_shared_graph
        mcp = init_mcp(
            runner_client=runner_client,
            runner_poller=None,  # wired after poller starts below
            health_store=store,
            agents=app.state.agents,
            graph=get_shared_graph(),
        )
        app.state.mcp = mcp
        logger.info("MCP living context interceptor initialised")
    except Exception:
        logger.warning("MCP interceptor init failed — running without living context")
        app.state.mcp = None

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

    # Wire poller into MCP interceptor now that it's running
    if getattr(app.state, "mcp", None) is not None:
        app.state.mcp._poller = runner_poller

    # Memory curator (enriches memories with categories, tiers, evolution)
    curation_store = MemoryCurationStore()
    curator = MemoryCurator(
        llm_fn=lambda prompt: app.state.agents["manager"].chat(
            prompt, task_context="memory curation"
        ),
        store=curation_store,
    )
    app.state.memory_curator = curator
    app.state.curation_store = curation_store
    logger.info("Memory curator initialised")

    # Discord notifier (Athena → user push notifications via webhook)
    discord_notifier = DiscordNotifier()
    app.state.discord_notifier = discord_notifier

    # Discord bot poller (user → Athena bidirectional comms)
    def _handle_discord_message(channel_id: str, text: str) -> str | None:
        """Forward Discord messages to Athena, return reply.

        The reply is sent back via the bot's channel.send() in discord.py.
        Do NOT also send via webhook — that creates a feedback loop
        (bot sees webhook message → treats as new user message → loop).
        """
        try:
            manager = app.state.agents.get("manager")
            if manager:
                reply = manager.chat(text, task_context="discord chat")
                if reply and not reply.startswith("Error:"):
                    return reply
                else:
                    logger.warning("Suppressed error reply to Discord: %s", reply[:120] if reply else "empty")
                    return "⚠️ Something went wrong — check server logs."
        except Exception:
            logger.exception("Discord message handler error")
        return None

    def _handle_discord_command(command: str, args: list[str]) -> str | None:
        """Handle !commands from Discord, return response text."""
        if command == "status":
            heartbeat = getattr(app.state, "heartbeat", None)
            if heartbeat:
                status = heartbeat.status()
                return (
                    f"📊 **Athena Status**\n"
                    f"Status: {status['drives']['status']}\n"
                    f"Energy: {status['drives']['energy']}\n"
                    f"Idle: {status['idle_seconds']:.0f}s\n"
                    f"Actions/hr: {status['actions_this_hour']}/{status['max_actions_per_hour']}"
                )
            return "Heartbeat not running"
        elif command == "tasks":
            tasks = task_store.list_by_column("backlog")
            if tasks:
                lines = [f"- [{t.priority}] {t.title}" for t in tasks[:10]]
                return "📋 **Backlog:**\n" + "\n".join(lines)
            return "Backlog is empty"
        elif command == "approve" and args:
            task_store.move(args[0], "done")
            return f"✅ Task {args[0]} approved → done"
        return None

    discord_poller = DiscordBotPoller(
        on_message=_handle_discord_message,
        on_command=_handle_discord_command,
    )
    app.state.discord_poller = discord_poller
    try:
        await discord_poller.start()
    except Exception:
        logger.warning("Discord bot poller failed to start")

    # Autonomous heartbeat (Athena picks tasks when idle)
    _consecutive_heartbeat_errors = 0
    MAX_HEARTBEAT_ERRORS = 3  # pause broadcasting after N consecutive errors

    def _heartbeat_action_callback(action: dict) -> None:
        """Broadcast heartbeat actions to Discord."""
        nonlocal _consecutive_heartbeat_errors
        action_type = action.get("type", "unknown")

        # Don't broadcast errors — prevents spam
        error = action.get("error")
        if error:
            _consecutive_heartbeat_errors += 1
            logger.warning(
                "Heartbeat error #%d: %s", _consecutive_heartbeat_errors, str(error)[:200]
            )
            if _consecutive_heartbeat_errors >= MAX_HEARTBEAT_ERRORS:
                logger.error(
                    "Heartbeat paused Discord notifications after %d consecutive errors",
                    _consecutive_heartbeat_errors,
                )
            return

        # Success → reset error counter
        _consecutive_heartbeat_errors = 0

        if action_type == "pick_task":
            import asyncio
            asyncio.ensure_future(
                discord_notifier.notify_task_started(
                    action.get("task_title", "?"),
                    action.get("reason", ""),
                )
            )
        elif action_type == "rest":
            discord_notifier.send_sync(f"💤 Resting: {action.get('reason', '')}")
    heartbeat = HeartbeatScheduler(
        manager=app.state.agents["manager"],
        task_store=task_store,
        tracker=tracker,
        agents=app.state.agents,
        on_action=_heartbeat_action_callback,
    )
    app.state.heartbeat = heartbeat
    try:
        await heartbeat.start()
        logger.info("Heartbeat scheduler started")
    except Exception:
        logger.exception("Heartbeat scheduler failed to start")

    yield

    # Shutdown
    if hasattr(app.state, "heartbeat"):
        await app.state.heartbeat.stop()
    if hasattr(app.state, "discord_poller"):
        await app.state.discord_poller.stop()
    if hasattr(app.state, "discord_notifier"):
        await app.state.discord_notifier.close()
    await runner_poller.stop()
    await scheduler.stop()
    store.close()
    task_store.close()
    curation_store.close()


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
    app.include_router(diag_router, prefix="/api")

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

