"""Pydantic models for runner API requests and responses."""

from __future__ import annotations

from pydantic import BaseModel

# ── Shared responses ─────────────────────────────────────────────────────────


class RunnerHealth(BaseModel):
    ok: bool
    name: str
    version: str
    platform: str
    timestamp: str


class CmdResult(BaseModel):
    exitCode: int
    stdout: str
    stderr: str
    durationMs: int


class GitStatus(BaseModel):
    branch: str
    lastCommit: dict[str, str]
    dirtyCount: int
    changedFiles: list[str]


class PrResult(BaseModel):
    prUrl: str


# ── Requests ─────────────────────────────────────────────────────────────────


class CmdRequest(BaseModel):
    projectId: str
    command: str
    timeoutSec: int = 1200


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
