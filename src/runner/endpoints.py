"""Runner API endpoints — health, cmd, git, claude, push-pr."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from src.runner import __version__
from src.runner.config import runner_settings
from src.runner.safety import SafetyError, validate_branch_for_push, validate_command

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Pydantic models ──────────────────────────────────────────────────────────


class CmdRequest(BaseModel):
    projectId: str
    command: str
    timeoutSec: int = 1200


class CmdResponse(BaseModel):
    exitCode: int
    stdout: str
    stderr: str
    durationMs: int


class ClaudeRunRequest(BaseModel):
    projectId: str
    model: str = "claude-sonnet-4-20250514"
    prompt: str
    dangerouslySkipPermissions: bool = False
    timeoutSec: int = 7200


class PushPrRequest(BaseModel):
    projectId: str
    branch: str
    base: str = "main"
    title: str
    body: str = ""
    remote: str = "origin"


class PushPrResponse(BaseModel):
    prUrl: str


class GitStatusResponse(BaseModel):
    branch: str
    lastCommit: dict[str, str]
    dirtyCount: int
    changedFiles: list[str]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _resolve_project_path(project_id: str, request: Request) -> Path:
    """Look up a project's local path from the registry."""
    registry = request.app.state.registry
    project = registry.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Unknown project: {project_id}")

    # Determine the local path
    path_str = ""
    if sys.platform == "win32" and project.local.path_windows:
        path_str = project.local.path_windows
    elif sys.platform == "linux" and project.local.path_linux:
        path_str = project.local.path_linux
    elif sys.platform == "darwin" and project.local.path_mac:
        path_str = project.local.path_mac

    # Fallback to repo_path
    if not path_str:
        path_str = project.repo_path

    if not path_str:
        raise HTTPException(
            status_code=400,
            detail=f"No local path configured for project '{project_id}' on {sys.platform}",
        )

    path = Path(path_str)
    if not path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Project path does not exist: {path}",
        )
    return path


def _run_subprocess(
    cmd: list[str],
    cwd: Path,
    timeout_sec: int,
    env: dict[str, str] | None = None,
) -> CmdResponse:
    """Run a subprocess safely (no shell) and return structured result."""
    t0 = time.perf_counter()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
        duration_ms = int((time.perf_counter() - t0) * 1000)
        return CmdResponse(
            exitCode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            durationMs=duration_ms,
        )
    except subprocess.TimeoutExpired:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        return CmdResponse(
            exitCode=-1,
            stdout="",
            stderr=f"Command timed out after {timeout_sec}s",
            durationMs=duration_ms,
        )
    except FileNotFoundError as e:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        return CmdResponse(
            exitCode=-1,
            stdout="",
            stderr=f"Command not found: {e}",
            durationMs=duration_ms,
        )
    except Exception as e:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        return CmdResponse(
            exitCode=-1,
            stdout="",
            stderr=f"Error: {type(e).__name__}: {e}",
            durationMs=duration_ms,
        )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/health")
def health_check() -> dict[str, Any]:
    """Runner health endpoint."""
    return {
        "ok": True,
        "name": "cla-runner",
        "version": __version__,
        "platform": sys.platform,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/usage")
def local_usage() -> dict[str, Any]:
    """Serve local Claude Code usage data (~/.claude) over the tunnel.

    The VPS cannot access ~/.claude directly — this endpoint exposes it
    through the reverse SSH tunnel so the dashboard shows real usage data.
    """
    try:
        from src.token_tracker.session_parser import (
            compute_rate_limits,
            parse_all_sessions,
            report_to_dict,
        )
        from src.config import settings

        report = parse_all_sessions()
        rate_limits = compute_rate_limits(
            session_cap=settings.session_limit_tokens,
            weekly_cap=settings.weekly_limit_tokens,
            session_window_hours=settings.session_window_hours,
            weekly_window_days=settings.weekly_window_days,
        )
        data = report_to_dict(report)
        data["rate_limits"] = rate_limits
        return {"ok": True, "data": data}
    except Exception as e:
        logger.warning("Usage data unavailable: %s", e)
        return {"ok": False, "error": str(e), "data": {}}


@router.post("/cmd", response_model=CmdResponse)
def run_command(req: CmdRequest, request: Request) -> CmdResponse:
    """Execute an arbitrary command in a project directory."""
    # Validate safety
    try:
        validate_command(req.command)
    except SafetyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    project_path = _resolve_project_path(req.projectId, request)
    timeout = min(req.timeoutSec, runner_settings.runner_command_timeout)

    # Split command for subprocess (supports both simple and complex commands)
    # On Windows, use cmd /c for complex commands; simple ones can be split
    if sys.platform == "win32":
        cmd = ["cmd", "/c", req.command]
    else:
        cmd = ["sh", "-c", req.command]

    logger.info("CMD [%s] in %s: %s", req.projectId, project_path, req.command)
    return _run_subprocess(cmd, project_path, timeout)


@router.get("/git/status", response_model=GitStatusResponse)
def git_status(
    projectId: str = Query(...),  # noqa: N803
    request: Request = None,  # type: ignore[assignment]
) -> GitStatusResponse:
    """Get git status for a project."""
    project_path = _resolve_project_path(projectId, request)

    # Get current branch
    branch_result = _run_subprocess(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], project_path, 10
    )
    branch = branch_result.stdout.strip() if branch_result.exitCode == 0 else "unknown"

    # Get last commit info
    log_result = _run_subprocess(
        [
            "git", "log", "-1",
            "--format=%H%n%s%n%an%n%aI",
        ],
        project_path,
        10,
    )
    last_commit: dict[str, str] = {"sha": "", "message": "", "author": "", "date": ""}
    if log_result.exitCode == 0:
        lines = log_result.stdout.strip().split("\n")
        if len(lines) >= 4:
            last_commit = {
                "sha": lines[0][:12],
                "message": lines[1],
                "author": lines[2],
                "date": lines[3],
            }

    # Get changed files
    status_result = _run_subprocess(
        ["git", "status", "--porcelain"], project_path, 10
    )
    changed_files: list[str] = []
    if status_result.exitCode == 0 and status_result.stdout.strip():
        for line in status_result.stdout.strip().split("\n"):
            if line.strip():
                # Format is "XY filename" — extract just the filename
                changed_files.append(line[3:].strip())

    return GitStatusResponse(
        branch=branch,
        lastCommit=last_commit,
        dirtyCount=len(changed_files),
        changedFiles=changed_files,
    )


@router.get("/git/diff")
def git_diff(
    projectId: str = Query(...),  # noqa: N803
    request: Request = None,  # type: ignore[assignment]
) -> PlainTextResponse:
    """Get unified diff for a project. Returns 413 if diff is too large."""
    project_path = _resolve_project_path(projectId, request)

    diff_result = _run_subprocess(["git", "diff"], project_path, 30)

    if diff_result.exitCode != 0:
        raise HTTPException(status_code=500, detail=f"git diff failed: {diff_result.stderr}")

    diff_text = diff_result.stdout

    # Also include staged changes
    staged_result = _run_subprocess(["git", "diff", "--cached"], project_path, 30)
    if staged_result.exitCode == 0 and staged_result.stdout:
        diff_text += "\n" + staged_result.stdout

    # Size limit
    max_bytes = runner_settings.runner_max_diff_bytes
    if len(diff_text.encode("utf-8", errors="replace")) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Diff too large ({len(diff_text)} chars). "
            f"Max allowed: {max_bytes} bytes. "
            "Use 'git diff -- <path>' for specific files.",
        )

    return PlainTextResponse(diff_text, media_type="text/plain")


@router.post("/claude/run", response_model=CmdResponse)
def run_claude(req: ClaudeRunRequest, request: Request) -> CmdResponse:
    """Execute Claude CLI in a project directory."""
    project_path = _resolve_project_path(req.projectId, request)
    timeout = min(req.timeoutSec, runner_settings.runner_claude_timeout)

    cmd = [runner_settings.claude_cli_path, "-p", req.prompt]

    if req.model:
        cmd.extend(["--model", req.model])

    if req.dangerouslySkipPermissions:
        cmd.append("--dangerously-skip-permissions")

    logger.info(
        "CLAUDE [%s] model=%s prompt_len=%d skip_perms=%s",
        req.projectId, req.model, len(req.prompt), req.dangerouslySkipPermissions,
    )
    return _run_subprocess(cmd, project_path, timeout)


@router.post("/git/push-pr", response_model=PushPrResponse)
def push_pr(req: PushPrRequest, request: Request) -> PushPrResponse:
    """Create a branch, commit staged changes, push, and open a PR."""
    # Safety: never push to main/master
    try:
        validate_branch_for_push(req.branch)
    except SafetyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    project_path = _resolve_project_path(req.projectId, request)

    # 1. Check if branch exists; create if not
    check_branch = _run_subprocess(
        ["git", "rev-parse", "--verify", req.branch], project_path, 10
    )
    if check_branch.exitCode != 0:
        # Create and checkout new branch
        create_result = _run_subprocess(
            ["git", "checkout", "-b", req.branch], project_path, 10
        )
        if create_result.exitCode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create branch: {create_result.stderr}",
            )
    else:
        # Checkout existing branch
        co_result = _run_subprocess(
            ["git", "checkout", req.branch], project_path, 10
        )
        if co_result.exitCode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to checkout branch: {co_result.stderr}",
            )

    # 2. Stage all changes
    stage_result = _run_subprocess(["git", "add", "-A"], project_path, 10)
    if stage_result.exitCode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to stage changes: {stage_result.stderr}",
        )

    # 3. Check if there's anything to commit
    status_result = _run_subprocess(
        ["git", "status", "--porcelain"], project_path, 10
    )
    if not status_result.stdout.strip():
        raise HTTPException(
            status_code=400,
            detail="Nothing to commit — working tree is clean.",
        )

    # 4. Commit
    commit_result = _run_subprocess(
        ["git", "commit", "-m", req.title], project_path, 30
    )
    if commit_result.exitCode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Commit failed: {commit_result.stderr}",
        )

    # 5. Push branch
    push_result = _run_subprocess(
        ["git", "push", "-u", req.remote, req.branch], project_path, 60
    )
    if push_result.exitCode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Push failed: {push_result.stderr}",
        )

    # 6. Create PR via GitHub CLI
    pr_cmd = [
        "gh", "pr", "create",
        "--base", req.base,
        "--head", req.branch,
        "--title", req.title,
    ]
    if req.body:
        pr_cmd.extend(["--body", req.body])
    else:
        pr_cmd.extend(["--body", f"Automated PR created by CLA runner.\n\nBranch: {req.branch}"])

    pr_result = _run_subprocess(pr_cmd, project_path, 30)

    if pr_result.exitCode != 0:
        # PR creation failed but push succeeded — return partial info
        raise HTTPException(
            status_code=500,
            detail=(
                f"Branch pushed successfully but PR creation failed. "
                f"Ensure 'gh' CLI is installed and authenticated.\n"
                f"Error: {pr_result.stderr}\n"
                f"You can create the PR manually."
            ),
        )

    # gh pr create outputs the PR URL as the last line
    pr_url = pr_result.stdout.strip().split("\n")[-1]

    return PushPrResponse(prUrl=pr_url)
