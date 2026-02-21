"""Control-plane API routes that proxy to the local runner.

These endpoints let the dashboard interact with the runner (commands, git, claude)
via the reverse SSH tunnel. If the runner is offline, they return a clear error.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.runner_connector.client import RunnerClient, RunnerError, RunnerOfflineError
from src.runner_connector.models import (
    ClaudeRunRequest,
    CmdRequest,
    PushPrRequest,
)

logger = logging.getLogger(__name__)

runner_router = APIRouter(prefix="/runner", tags=["runner"])


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_client(request: Request) -> RunnerClient:
    """Get the RunnerClient from app state."""
    return request.app.state.runner_client  # type: ignore[no-any-return]


def _require_online(request: Request) -> None:
    """Raise 503 if runner is offline."""
    poller = request.app.state.runner_poller
    if not poller.state.online:
        raise HTTPException(
            status_code=503,
            detail=(
                "Runner is offline. Start the runner on your local machine"
                " and open the SSH tunnel."
            ),
        )


# ── Request models ───────────────────────────────────────────────────────────


class RunCmdBody(BaseModel):
    projectId: str
    command: str
    timeoutSec: int = 1200


class RunClaudeBody(BaseModel):
    projectId: str
    model: str = "claude-sonnet-4-20250514"
    prompt: str
    dangerouslySkipPermissions: bool = False
    timeoutSec: int = 7200


class PushPrBody(BaseModel):
    projectId: str
    branch: str
    base: str = "main"
    title: str
    body: str = ""
    remote: str = "origin"


# ── Endpoints ────────────────────────────────────────────────────────────────


@runner_router.get("/status")
def runner_status(request: Request) -> dict[str, Any]:
    """Get current runner online/offline status."""
    poller = request.app.state.runner_poller
    return poller.state.to_dict()


@runner_router.get("/debug")
def runner_debug(request: Request) -> dict[str, Any]:
    """Debug runner connectivity — returns the exact error when offline.

    Useful for diagnosing SSH tunnel issues without reading server logs.
    Visit /api/runner/debug in your browser to see what's failing.
    """
    from src.config import settings

    client = _get_client(request)
    poller = request.app.state.runner_poller

    result: dict[str, Any] = {
        "runner_base_url": settings.runner_base_url,
        "poller_state": poller.state.to_dict(),
    }
    try:
        health = client.health()
        result["health"] = {"ok": True, "version": health.version, "platform": health.platform}
    except RunnerOfflineError as e:
        result["health"] = {
            "ok": False,
            "error": str(e),
            "hint": "Is companion-runner running? Is the SSH tunnel open?",
        }
    except RunnerError as e:
        result["health"] = {"ok": False, "error": f"HTTP {e.status_code}: {e.detail}"}
    except Exception as e:
        result["health"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return result


@runner_router.get("/usage")
def runner_usage(request: Request) -> dict[str, Any]:
    """Proxy local ~/.claude usage data from the runner through the tunnel.

    When runner is online, returns real token usage from your local machine.
    When offline, returns empty so the dashboard shows 0 instead of an error.
    """
    poller = request.app.state.runner_poller
    if not poller.state.online:
        return {"ok": False, "online": False, "data": {}}

    client = _get_client(request)
    try:
        data = client.usage()
        return {"ok": True, "online": True, **data}
    except (RunnerOfflineError, RunnerError) as e:
        return {"ok": False, "online": False, "error": str(e), "data": {}}


@runner_router.post("/cmd")
def proxy_cmd(body: RunCmdBody, request: Request) -> dict[str, Any]:
    """Execute a command on the runner."""
    _require_online(request)
    client = _get_client(request)

    try:
        result = client.run_cmd(CmdRequest(
            projectId=body.projectId,
            command=body.command,
            timeoutSec=body.timeoutSec,
        ))
        return result.model_dump()
    except RunnerOfflineError:
        raise HTTPException(status_code=503, detail="Runner went offline during request")
    except RunnerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@runner_router.get("/git/status")
def proxy_git_status(projectId: str, request: Request) -> dict[str, Any]:  # noqa: N803
    """Get git status from the runner."""
    _require_online(request)
    client = _get_client(request)

    try:
        result = client.git_status(projectId)
        return result.model_dump()
    except RunnerOfflineError:
        raise HTTPException(status_code=503, detail="Runner went offline during request")
    except RunnerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@runner_router.get("/git/diff")
def proxy_git_diff(projectId: str, request: Request) -> dict[str, str]:  # noqa: N803
    """Get git diff from the runner."""
    _require_online(request)
    client = _get_client(request)

    try:
        diff_text = client.git_diff(projectId)
        return {"diff": diff_text}
    except RunnerOfflineError:
        raise HTTPException(status_code=503, detail="Runner went offline during request")
    except RunnerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@runner_router.post("/claude/run")
def proxy_claude_run(body: RunClaudeBody, request: Request) -> dict[str, Any]:
    """Run Claude CLI on the runner."""
    _require_online(request)
    client = _get_client(request)

    try:
        result = client.run_claude(ClaudeRunRequest(
            projectId=body.projectId,
            model=body.model,
            prompt=body.prompt,
            dangerouslySkipPermissions=body.dangerouslySkipPermissions,
            timeoutSec=body.timeoutSec,
        ))
        return result.model_dump()
    except RunnerOfflineError:
        raise HTTPException(status_code=503, detail="Runner went offline during request")
    except RunnerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@runner_router.post("/git/push-pr")
def proxy_push_pr(body: PushPrBody, request: Request) -> dict[str, Any]:
    """Create a PR via the runner."""
    _require_online(request)
    client = _get_client(request)

    try:
        result = client.push_pr(PushPrRequest(
            projectId=body.projectId,
            branch=body.branch,
            base=body.base,
            title=body.title,
            body=body.body,
            remote=body.remote,
        ))
        return result.model_dump()
    except RunnerOfflineError:
        raise HTTPException(status_code=503, detail="Runner went offline during request")
    except RunnerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@runner_router.get("/dev-state/{project_id}")
def get_dev_state(project_id: str, request: Request) -> dict[str, Any]:
    """Get combined dev state for a project (git status from runner).

    Returns 'offline' status if runner is not available, instead of erroring.
    """
    poller = request.app.state.runner_poller
    if not poller.state.online:
        return {
            "project_id": project_id,
            "status": "offline",
            "message": "Runner is offline — dev state unavailable",
        }

    client = _get_client(request)
    try:
        git = client.git_status(project_id)
        return {
            "project_id": project_id,
            "status": "online",
            "branch": git.branch,
            "lastCommit": git.lastCommit,
            "dirtyCount": git.dirtyCount,
            "changedFiles": git.changedFiles,
        }
    except RunnerOfflineError:
        return {
            "project_id": project_id,
            "status": "offline",
            "message": "Runner went offline",
        }
    except RunnerError as e:
        return {
            "project_id": project_id,
            "status": "error",
            "message": e.detail,
        }


# ── Streaming endpoints (SSE) ───────────────────────────────────────────

class StreamCmdBody(BaseModel):
    projectId: str
    command: str
    timeoutSec: int = 1200


@runner_router.post("/cmd/stream")
async def stream_cmd(body: StreamCmdBody, request: Request):
    """Execute a command on the runner and stream progress via SSE.

    Since the runner itself returns the full result synchronously,
    this wraps it with progress events so the UI can show real-time status.

    Events:
      event: start    data: {"command":"...","projectId":"..."}
      event: running  data: {"elapsed":N}
      event: output   data: {"stdout":"...","stderr":"...","exitCode":N}
      event: error    data: {"detail":"..."}
      event: done     data: {}
    """
    _require_online(request)
    client = _get_client(request)

    async def event_generator():
        yield f"event: start\ndata: {json.dumps({'command': body.command, 'projectId': body.projectId})}\n\n"

        # We run the command in a thread and poll for completion
        import time
        t0 = time.time()
        result_holder = {"done": False, "result": None, "error": None}

        def _execute():
            try:
                r = client.run_cmd(CmdRequest(
                    projectId=body.projectId,
                    command=body.command,
                    timeoutSec=body.timeoutSec,
                ))
                result_holder["result"] = r.model_dump()
            except RunnerOfflineError:
                result_holder["error"] = "Runner went offline during request"
            except RunnerError as e:
                result_holder["error"] = e.detail
            except Exception as e:
                result_holder["error"] = str(e)
            finally:
                result_holder["done"] = True

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _execute)

        # Send progress updates every 2s while waiting
        while not result_holder["done"]:
            elapsed = round(time.time() - t0, 1)
            yield f"event: running\ndata: {json.dumps({'elapsed': elapsed})}\n\n"
            await asyncio.sleep(2)

        if result_holder["error"]:
            yield f"event: error\ndata: {json.dumps({'detail': result_holder['error']})}\n\n"
        else:
            yield f"event: output\ndata: {json.dumps(result_holder['result'])}\n\n"

        yield f"event: done\ndata: {{}}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
