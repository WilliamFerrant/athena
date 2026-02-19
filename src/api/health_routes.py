"""API routes for the Project Registry + Health Engine.

Endpoints:
  GET  /api/projects              — list all projects grouped by category
  GET  /api/projects/{id}         — project detail + latest health
  POST /api/projects/{id}/check   — trigger immediate health check
  GET  /api/health/summary        — all projects with badge status
  GET  /api/health/history/{project_id}/{check_id} — time series
  GET  /api/health/incidents      — open + recent incidents
  GET  /api/health/stream         — SSE stream of live check results
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

health_router = APIRouter()

# ── SSE subscriber list (in-memory) ──────────────────────────────────────────

_sse_queues: list[asyncio.Queue[dict[str, Any]]] = []


def broadcast_result(result: Any) -> None:
    """Push a check result to all SSE subscribers."""
    data = {
        "project_id": result.project_id,
        "check_id": result.check_id,
        "check_type": result.check_type,
        "status": result.status.value,
        "latency_ms": result.latency_ms,
        "message": result.message,
        "timestamp": result.timestamp,
    }
    for q in _sse_queues:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass  # slow consumer — drop


# ── Project endpoints ────────────────────────────────────────────────────────


@health_router.get("/projects")
def list_projects(request: Request) -> dict[str, Any]:
    """List all projects from registry, grouped by category."""
    registry = request.app.state.registry
    store = request.app.state.health_store

    projects = registry.to_dict()
    latest = store.get_all_latest()

    # Attach health status to each project
    for p in projects:
        checks = latest.get(p["id"], [])
        statuses = [c["status"] for c in checks]
        if not statuses:
            p["health"] = "unknown"
        elif "down" in statuses:
            p["health"] = "down"
        elif "degraded" in statuses:
            p["health"] = "degraded"
        else:
            p["health"] = "up"
        p["checks"] = checks

    # Group by category
    groups: dict[str, list[dict[str, Any]]] = {}
    for p in projects:
        g = p.get("group", "other")
        groups.setdefault(g, []).append(p)

    return {"projects": projects, "groups": groups}


@health_router.get("/projects/{project_id}")
def get_project(project_id: str, request: Request) -> dict[str, Any]:
    """Get project detail with latest health checks + dev state from runner."""
    registry = request.app.state.registry
    store = request.app.state.health_store

    project = registry.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

    from src.projects.registry import _project_to_dict
    proj_dict = _project_to_dict(project)

    # Attach health status for each check (prod health)
    health_checks = []
    for check in project.health_checks:
        latest = store.get_latest(project_id, check.id)
        uptime = store.get_uptime_24h(project_id, check.id)
        health_checks.append({
            "check_id": check.id,
            "type": check.type,
            "latest": latest,
            "uptime_24h": uptime,
        })

    proj_dict["health_checks_status"] = health_checks
    proj_dict["incidents"] = store.get_incidents(project_id, limit=10)

    # Attach dev state from runner (if online)
    try:
        from src.runner_connector.client import RunnerClient, RunnerOfflineError, RunnerError
        poller = request.app.state.runner_poller
        if poller.state.online:
            client: RunnerClient = request.app.state.runner_client
            git = client.git_status(project_id)
            proj_dict["dev_state"] = {
                "status": "online",
                "branch": git.branch,
                "lastCommit": git.lastCommit,
                "dirtyCount": git.dirtyCount,
                "changedFiles": git.changedFiles,
            }
        else:
            proj_dict["dev_state"] = {
                "status": "offline",
                "message": "Runner is offline — dev state unavailable",
            }
    except Exception as e:
        proj_dict["dev_state"] = {
            "status": "error",
            "message": str(e),
        }

    return proj_dict


@health_router.post("/projects/{project_id}/check")
async def trigger_check(project_id: str, request: Request) -> dict[str, Any]:
    """Trigger immediate health check for a project."""
    scheduler = request.app.state.health_scheduler
    registry = request.app.state.registry

    project = registry.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

    results = await scheduler.run_project_checks(project_id)
    return {
        "project_id": project_id,
        "results": [
            {
                "check_id": r.check_id,
                "status": r.status.value,
                "latency_ms": r.latency_ms,
                "message": r.message,
            }
            for r in results
        ],
    }


# ── Health endpoints ─────────────────────────────────────────────────────────


@health_router.get("/health/summary")
def health_summary(request: Request) -> dict[str, Any]:
    """Get overall health summary with badge status per project."""
    registry = request.app.state.registry
    store = request.app.state.health_store

    latest = store.get_all_latest()
    open_incidents = store.get_open_incidents()

    summary = []
    for p in registry.load():
        checks = latest.get(p.id, [])
        statuses = [c["status"] for c in checks]

        if not statuses:
            overall = "unknown"
        elif "down" in statuses:
            overall = "down"
        elif "degraded" in statuses:
            overall = "degraded"
        else:
            overall = "up"

        project_incidents = [i for i in open_incidents if i["project_id"] == p.id]

        summary.append({
            "project_id": p.id,
            "name": p.name,
            "group": p.group,
            "status": overall,
            "checks": checks,
            "open_incidents": len(project_incidents),
        })

    return {"summary": summary, "total_incidents": len(open_incidents)}


@health_router.get("/health/history/{project_id}/{check_id}")
def check_history(
    project_id: str, check_id: str, limit: int = 100, request: Request = None,
) -> dict[str, Any]:
    """Get time-series history for a specific check."""
    store = request.app.state.health_store
    history = store.get_history(project_id, check_id, limit)
    uptime = store.get_uptime_24h(project_id, check_id)
    return {
        "project_id": project_id,
        "check_id": check_id,
        "uptime_24h": uptime,
        "history": history,
    }


@health_router.get("/health/incidents")
def list_incidents(
    project_id: str | None = None, limit: int = 50, request: Request = None,
) -> dict[str, Any]:
    """Get incidents (open + resolved)."""
    store = request.app.state.health_store
    return {
        "incidents": store.get_incidents(project_id, limit),
        "open": store.get_open_incidents(),
    }


# ── SSE stream ───────────────────────────────────────────────────────────────


@health_router.get("/health/stream")
async def health_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events stream for real-time health check results."""
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50)
    _sse_queues.append(queue)

    async def event_generator():
        try:
            # Send initial state
            store = request.app.state.health_store
            latest = store.get_all_latest()
            yield f"event: init\ndata: {json.dumps(latest)}\n\n"

            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"event: check\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield f": keepalive\n\n"
        finally:
            _sse_queues.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
