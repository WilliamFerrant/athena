"""Tests for the local runner — auth, endpoints, safety, contract serialization."""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.runner.app import create_runner_app
from src.runner.safety import SafetyError, validate_branch_for_push, validate_command

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def runner_app_no_auth() -> Any:
    """Runner app with no auth token (dev mode)."""
    with patch("src.runner.app.runner_settings") as mock_settings:
        mock_settings.runner_token = ""
        mock_settings.runner_projects_file = "projects.yaml"
        mock_settings.log_level = "WARNING"
        app = create_runner_app()
    return app


@pytest.fixture
def runner_app_with_auth() -> Any:
    """Runner app with auth token."""
    app = create_runner_app()
    return app


@pytest.fixture
def client_no_auth(runner_app_no_auth: Any) -> Generator[TestClient, None, None]:
    """Test client with no auth required."""
    # Mock the registry with a proper MagicMock
    mock_registry = MagicMock()
    mock_registry.get = MagicMock(return_value=None)
    mock_registry.load = MagicMock(return_value=[])
    mock_registry._projects = []
    runner_app_no_auth.state.registry = mock_registry
    with TestClient(runner_app_no_auth) as client:
        # Re-set after lifespan may have overwritten
        runner_app_no_auth.state.registry = mock_registry
        yield client


# ── Auth tests ───────────────────────────────────────────────────────────────


class TestRunnerAuth:
    """Test X-Runner-Token authentication."""

    def test_no_auth_dev_mode_allows_requests(self, client_no_auth: TestClient) -> None:
        """When no token is configured, all requests should pass."""
        resp = client_no_auth.get("/health")
        assert resp.status_code == 200

    def test_auth_rejects_missing_token(self) -> None:
        """When token is set, requests without it should be rejected."""
        with patch("src.runner.app.runner_settings") as mock_settings:
            mock_settings.runner_token = "secret-token-123"
            mock_settings.runner_projects_file = "projects.yaml"
            mock_settings.log_level = "WARNING"
            app = create_runner_app()
            app.state.registry = MagicMock()

            with TestClient(app) as client:
                resp = client.get("/health")
                assert resp.status_code == 401
                assert "X-Runner-Token" in resp.json()["detail"]

    def test_auth_rejects_wrong_token(self) -> None:
        """Wrong token should be rejected."""
        with patch("src.runner.app.runner_settings") as mock_settings:
            mock_settings.runner_token = "secret-token-123"
            mock_settings.runner_projects_file = "projects.yaml"
            mock_settings.log_level = "WARNING"
            app = create_runner_app()
            app.state.registry = MagicMock()

            with TestClient(app) as client:
                resp = client.get("/health", headers={"X-Runner-Token": "wrong-token"})
                assert resp.status_code == 401

    def test_auth_accepts_correct_token(self) -> None:
        """Correct token should be accepted."""
        with patch("src.runner.app.runner_settings") as mock_settings:
            mock_settings.runner_token = "secret-token-123"
            mock_settings.runner_projects_file = "projects.yaml"
            mock_settings.log_level = "WARNING"
            app = create_runner_app()
            app.state.registry = MagicMock()

            with TestClient(app) as client:
                resp = client.get("/health", headers={"X-Runner-Token": "secret-token-123"})
                assert resp.status_code == 200


# ── Health endpoint tests ────────────────────────────────────────────────────


class TestHealthEndpoint:
    """Test GET /health contract."""

    def test_health_returns_correct_schema(self, client_no_auth: TestClient) -> None:
        resp = client_no_auth.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["name"] == "cla-runner"
        assert "version" in data
        assert data["platform"] == sys.platform
        assert "timestamp" in data

    def test_health_timestamp_is_iso(self, client_no_auth: TestClient) -> None:
        resp = client_no_auth.get("/health")
        from datetime import datetime
        ts = resp.json()["timestamp"]
        # Should parse without error
        datetime.fromisoformat(ts)


# ── Safety tests ─────────────────────────────────────────────────────────────


class TestSafety:
    """Test command blocklist and branch protection."""

    def test_block_push_to_main(self) -> None:
        with pytest.raises(SafetyError, match="main/master"):
            validate_command("git push origin main")

    def test_block_push_to_master(self) -> None:
        with pytest.raises(SafetyError, match="main/master"):
            validate_command("git push origin master")

    def test_allow_push_to_feature_branch(self) -> None:
        # Should not raise
        validate_command("git push origin cla/feature-xyz")

    def test_block_merge_to_main(self) -> None:
        with pytest.raises(SafetyError, match="merging"):
            validate_command("git merge main")

    def test_block_destructive_rm(self) -> None:
        with pytest.raises(SafetyError, match="destructive"):
            validate_command("rm -rf /")

    def test_block_deploy_commands(self) -> None:
        with pytest.raises(SafetyError, match="deployment"):
            validate_command("vercel --prod")

    def test_allow_safe_commands(self) -> None:
        # These should all pass without raising
        validate_command("npm run dev")
        validate_command("npm run lint")
        validate_command("git status")
        validate_command("python -m pytest tests/ -v")

    def test_branch_protection_blocks_main(self) -> None:
        with pytest.raises(SafetyError, match="protected branch"):
            validate_branch_for_push("main")

    def test_branch_protection_blocks_master(self) -> None:
        with pytest.raises(SafetyError, match="protected branch"):
            validate_branch_for_push("master")

    def test_branch_protection_allows_feature(self) -> None:
        # Should not raise
        validate_branch_for_push("cla/my-feature")


# ── Contract serialization tests ─────────────────────────────────────────────


class TestContractSerialization:
    """Test that response models serialize correctly per the API contract."""

    def test_cmd_response_schema(self) -> None:
        from src.runner.endpoints import CmdResponse
        resp = CmdResponse(exitCode=0, stdout="ok", stderr="", durationMs=100)
        data = resp.model_dump()
        assert data == {"exitCode": 0, "stdout": "ok", "stderr": "", "durationMs": 100}

    def test_git_status_response_schema(self) -> None:
        from src.runner.endpoints import GitStatusResponse
        resp = GitStatusResponse(
            branch="feature/x",
            lastCommit={"sha": "abc123", "message": "msg", "author": "dev", "date": "2025-01-01"},
            dirtyCount=2,
            changedFiles=["a.ts", "b.ts"],
        )
        data = resp.model_dump()
        assert data["branch"] == "feature/x"
        assert data["dirtyCount"] == 2
        assert len(data["changedFiles"]) == 2

    def test_push_pr_response_schema(self) -> None:
        from src.runner.endpoints import PushPrResponse
        resp = PushPrResponse(prUrl="https://github.com/user/repo/pull/42")
        assert resp.model_dump() == {"prUrl": "https://github.com/user/repo/pull/42"}

    def test_cmd_request_defaults(self) -> None:
        from src.runner.endpoints import CmdRequest
        req = CmdRequest(projectId="test", command="npm run dev")
        assert req.timeoutSec == 1200

    def test_claude_run_request_defaults(self) -> None:
        from src.runner.endpoints import ClaudeRunRequest
        req = ClaudeRunRequest(projectId="test", prompt="Do something")
        assert req.dangerouslySkipPermissions is False
        assert req.timeoutSec == 7200


# ── Diff size limiting tests ────────────────────────────────────────────────


class TestDiffSizeLimiting:
    """Test that large diffs are rejected with 413."""

    def test_diff_too_large_returns_413(self, client_no_auth: TestClient) -> None:
        """Simulate a diff that exceeds the size limit."""
        mock_registry = client_no_auth.app.state.registry  # type: ignore[union-attr]
        mock_project = MagicMock()
        mock_project.local.path_windows = str(Path(__file__).parent.parent)
        mock_project.local.path_linux = ""
        mock_project.local.path_mac = ""
        mock_project.repo_path = str(Path(__file__).parent.parent)
        mock_registry.get.return_value = mock_project

        # Patch subprocess to return a giant diff
        large_diff = "x" * 600_000  # Exceeds 500KB default limit
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = large_diff
        mock_result.stderr = ""

        with patch("src.runner.endpoints.subprocess.run", return_value=mock_result):
            with patch("src.runner.endpoints.runner_settings") as mock_settings:
                mock_settings.runner_max_diff_bytes = 500_000
                resp = client_no_auth.get("/git/diff?projectId=test-project")
                assert resp.status_code == 413

    def test_small_diff_returns_200(self, client_no_auth: TestClient) -> None:
        """Small diffs should return normally."""
        mock_registry = client_no_auth.app.state.registry  # type: ignore[union-attr]
        mock_project = MagicMock()
        mock_project.local.path_windows = str(Path(__file__).parent.parent)
        mock_project.local.path_linux = ""
        mock_project.local.path_mac = ""
        mock_project.repo_path = str(Path(__file__).parent.parent)
        mock_registry.get.return_value = mock_project

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "small diff"
        mock_result.stderr = ""

        with patch("src.runner.endpoints.subprocess.run", return_value=mock_result):
            with patch("src.runner.endpoints.runner_settings") as mock_settings:
                mock_settings.runner_max_diff_bytes = 500_000
                resp = client_no_auth.get("/git/diff?projectId=test-project")
                assert resp.status_code == 200
                assert "small diff" in resp.text


# ── Command execution tests ──────────────────────────────────────────────────


class TestCommandEndpoint:
    """Test POST /cmd with mocked subprocess."""

    def test_cmd_blocked_command_returns_400(self, client_no_auth: TestClient) -> None:
        """Blocked commands should return 400."""
        resp = client_no_auth.post("/cmd", json={
            "projectId": "test",
            "command": "git push origin main",
        })
        assert resp.status_code == 400
        assert "main/master" in resp.json()["detail"]

    def test_cmd_unknown_project_returns_404(self, client_no_auth: TestClient) -> None:
        """Unknown project should return 404."""
        mock_registry = client_no_auth.app.state.registry  # type: ignore[union-attr]
        mock_registry.get.return_value = None

        resp = client_no_auth.post("/cmd", json={
            "projectId": "nonexistent",
            "command": "npm run lint",
        })
        assert resp.status_code == 404


# ── Connector model tests ───────────────────────────────────────────────────


class TestConnectorModels:
    """Test runner connector Pydantic models."""

    def test_runner_health_model(self) -> None:
        from src.runner_connector.models import RunnerHealth
        h = RunnerHealth(
            ok=True, name="cla-runner", version="0.1.0",
            platform="win32", timestamp="2025-01-01T00:00:00Z",
        )
        assert h.ok is True
        assert h.name == "cla-runner"

    def test_cmd_result_model(self) -> None:
        from src.runner_connector.models import CmdResult
        r = CmdResult(exitCode=0, stdout="ok", stderr="", durationMs=55)
        assert r.exitCode == 0

    def test_git_status_model(self) -> None:
        from src.runner_connector.models import GitStatus
        g = GitStatus(
            branch="main",
            lastCommit={"sha": "abc", "message": "init", "author": "test", "date": "now"},
            dirtyCount=0,
            changedFiles=[],
        )
        assert g.branch == "main"
        assert g.dirtyCount == 0
