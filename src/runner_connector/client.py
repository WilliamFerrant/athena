"""httpx-based client for the local runner API.

All methods return typed responses or raise RunnerOfflineError / RunnerError.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.runner_connector.models import (
    ClaudeRunRequest,
    CmdRequest,
    CmdResult,
    GitStatus,
    PrResult,
    PushPrRequest,
    RunnerHealth,
)

logger = logging.getLogger(__name__)


class RunnerOfflineError(Exception):
    """Raised when the runner is unreachable."""


class RunnerError(Exception):
    """Raised when the runner returns an error."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Runner error {status_code}: {detail}")


class RunnerClient:
    """Synchronous httpx client for the CLA local runner."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._token:
            h["X-Runner-Token"] = self._token
        return h

    def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Perform a GET request to the runner."""
        try:
            with httpx.Client(timeout=timeout or self._timeout) as client:
                resp = client.get(
                    f"{self._base_url}{path}",
                    headers=self._headers,
                    params=params,
                )
            if resp.status_code >= 400:
                detail = resp.text
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:
                    pass
                raise RunnerError(resp.status_code, str(detail))
            return resp
        except httpx.ConnectError:
            raise RunnerOfflineError("Runner is offline or unreachable")
        except httpx.TimeoutException:
            raise RunnerOfflineError("Runner request timed out")

    def _post(
        self,
        path: str,
        json_data: dict[str, Any],
        timeout: float | None = None,
    ) -> httpx.Response:
        """Perform a POST request to the runner."""
        try:
            with httpx.Client(timeout=timeout or self._timeout) as client:
                resp = client.post(
                    f"{self._base_url}{path}",
                    headers=self._headers,
                    json=json_data,
                )
            if resp.status_code >= 400:
                detail = resp.text
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:
                    pass
                raise RunnerError(resp.status_code, str(detail))
            return resp
        except httpx.ConnectError:
            raise RunnerOfflineError("Runner is offline or unreachable")
        except httpx.TimeoutException:
            raise RunnerOfflineError("Runner request timed out")

    # ── High-level methods ───────────────────────────────────────────────

    def health(self) -> RunnerHealth:
        """GET /health"""
        resp = self._get("/health", timeout=5.0)
        return RunnerHealth(**resp.json())

    def run_cmd(self, req: CmdRequest) -> CmdResult:
        """POST /cmd"""
        resp = self._post("/cmd", req.model_dump(), timeout=float(req.timeoutSec) + 10)
        return CmdResult(**resp.json())

    def git_status(self, project_id: str) -> GitStatus:
        """GET /git/status?projectId=..."""
        resp = self._get("/git/status", params={"projectId": project_id})
        return GitStatus(**resp.json())

    def git_diff(self, project_id: str) -> str:
        """GET /git/diff?projectId=... — returns plain text."""
        resp = self._get("/git/diff", params={"projectId": project_id}, timeout=60.0)
        return resp.text

    def run_claude(self, req: ClaudeRunRequest) -> CmdResult:
        """POST /claude/run"""
        resp = self._post(
            "/claude/run", req.model_dump(), timeout=float(req.timeoutSec) + 30
        )
        return CmdResult(**resp.json())

    def push_pr(self, req: PushPrRequest) -> PrResult:
        """POST /git/push-pr"""
        resp = self._post("/git/push-pr", req.model_dump(), timeout=120.0)
        return PrResult(**resp.json())

    def usage(self) -> dict[str, Any]:
        """GET /usage — fetch local ~/.claude usage data through the tunnel."""
        resp = self._get("/usage", timeout=15.0)
        return resp.json()
