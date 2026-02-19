"""Control-plane API routes that proxy to the local runner.

These endpoints let the dashboard interact with the runner (commands, git, claude)
via the reverse SSH tunnel. If the runner is offline, they return a clear error.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
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
