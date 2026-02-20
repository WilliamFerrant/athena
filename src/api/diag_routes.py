"""Diagnostics API routes — system health, predictive analytics, model switching.

Endpoints:
  GET  /api/diagnostics          — full system diagnostics snapshot
  GET  /api/diagnostics/forecast — predictive token usage forecast
  POST /api/diagnostics/model    — dynamic model switching at runtime
  GET  /api/diagnostics/notifications — notification channel status
  POST /api/diagnostics/notify/test  — send a test notification
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.config import settings

logger = logging.getLogger(__name__)

diag_router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


# ── Full diagnostics snapshot ────────────────────────────────────────────────


@diag_router.get("")
def system_diagnostics(request: Request) -> dict[str, Any]:
    """Full system diagnostics including offline status, agent health, etc."""
    tracker = request.app.state.tracker
    poller = request.app.state.runner_poller
    agents = getattr(request.app.state, "agents", {})

    # Agent diagnostics
    agent_diag = {}
    for aid, agent in agents.items():
        drives = agent.drives.state
        backend_name = type(agent.llm_backend).__name__
        agent_diag[aid] = {
            "type": agent.agent_type,
            "llm_backend": backend_name,
            "model": agent.default_model,
            "effectiveness": round(drives.overall_effectiveness(), 3),
            "status": drives.status_label(),
            "conversation_length": len(agent._conversation),
            "drives": drives.to_dict(),
        }

    # Runner diagnostics
    runner_diag = poller.state.to_dict()

    # Health store diagnostics
    store = getattr(request.app.state, "health_store", None)
    health_diag = {}
    if store:
        try:
            open_incidents = store.get_open_incidents()
            health_diag = {
                "open_incidents": len(open_incidents),
                "all_latest": store.get_all_latest(),
            }
        except Exception as e:
            health_diag = {"error": str(e)}

    # Token budget
    budget_diag = {
        "calls_made": len(tracker.records),
        "budget_remaining": tracker.budget_remaining,
        "is_over_budget": tracker.is_over_budget,
        "rate_limits": tracker.get_rate_limits(),
    }

    # Notifications
    try:
        from src.notifications import get_notifier
        notif_status = get_notifier().status()
    except Exception:
        notif_status = {"enabled": False}

    return {
        "status": "ok",
        "agents": agent_diag,
        "runner": runner_diag,
        "health": health_diag,
        "budget": budget_diag,
        "notifications": notif_status,
        "config": {
            "manager_backend": settings.manager_backend,
            "default_model": settings.default_model,
            "manager_model": settings.manager_model,
            "openai_model": settings.openai_model,
            "openai_configured": bool(settings.openai_api_key),
        },
    }


# ── Predictive forecast ──────────────────────────────────────────────────────


@diag_router.get("/forecast")
def get_forecast(request: Request) -> dict[str, Any]:
    """Predictive token usage analytics — trends, projections, recommendations."""
    try:
        from src.token_tracker.predictive import compute_forecast, forecast_to_dict
        forecast = compute_forecast(
            session_cap=settings.session_limit_tokens,
            weekly_cap=settings.weekly_limit_tokens,
        )
        return forecast_to_dict(forecast)
    except Exception as e:
        logger.exception("Forecast computation failed")
        return {"error": str(e), "trend": "error", "recommendations": []}


# ── Dynamic model switching ──────────────────────────────────────────────────


class ModelSwitchRequest(BaseModel):
    agent: str  # "manager", "frontend", "backend", "tester", or "all"
    model: str  # e.g. "opus", "sonnet", "gpt-4o", "gpt-4o-mini"


@diag_router.post("/model")
def switch_model(req: ModelSwitchRequest, request: Request) -> dict[str, Any]:
    """Dynamically switch the LLM model for an agent (or all agents) at runtime.

    For agents using Claude CLI, this changes the --model flag.
    For the Manager with OpenAI backend, this changes the OpenAI model.
    """
    agents = getattr(request.app.state, "agents", {})

    if req.agent == "all":
        targets = list(agents.keys())
    else:
        targets = [req.agent]

    results = {}
    for aid in targets:
        agent = agents.get(aid)
        if not agent:
            results[aid] = {"error": f"Unknown agent: {aid}"}
            continue

        old_model = agent.default_model
        agent.default_model = req.model

        # If agent has an OpenAI backend, update its model too
        if agent._llm_backend and hasattr(agent._llm_backend, "model"):
            agent._llm_backend.model = req.model

        results[aid] = {
            "old_model": old_model,
            "new_model": req.model,
            "backend": type(agent.llm_backend).__name__,
        }

    return {"switched": results}


# ── Notification endpoints ───────────────────────────────────────────────────


@diag_router.get("/notifications")
def notification_status() -> dict[str, Any]:
    """Get notification channel configuration status."""
    try:
        from src.notifications import get_notifier
        return get_notifier().status()
    except Exception as e:
        return {"enabled": False, "error": str(e)}


class TestNotifyRequest(BaseModel):
    message: str = "Test notification from Athena"


@diag_router.post("/notify/test")
async def send_test_notification(req: TestNotifyRequest) -> dict[str, str]:
    """Send a test notification to all configured channels."""
    try:
        from src.notifications import get_notifier, NotifyLevel
        notifier = get_notifier()
        if not notifier.is_enabled:
            raise HTTPException(status_code=400, detail="No notification channels configured")
        await notifier.send_custom(req.message, NotifyLevel.INFO)
        return {"status": "sent"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
