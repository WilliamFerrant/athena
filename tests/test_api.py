"""Tests for the FastAPI routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_app
from src.runner_connector.client import RunnerClient
from src.runner_connector.poller import RunnerPoller, RunnerState
from src.token_tracker.tracker import TokenTracker


@pytest.fixture
def client(mock_claude_cli):
    app = create_app()
    tracker = TokenTracker()
    app.state.tracker = tracker
    # Create agent pool matching server lifespan
    from src.agents.frontend import FrontendAgent
    from src.agents.backend import BackendAgent
    from src.agents.manager import ManagerAgent
    from src.agents.tester import TesterAgent
    app.state.agents = {
        "manager": ManagerAgent(agent_id="manager", tracker=tracker),
        "frontend": FrontendAgent(agent_id="frontend", tracker=tracker),
        "backend": BackendAgent(agent_id="backend", tracker=tracker),
        "tester": TesterAgent(agent_id="tester", tracker=tracker),
    }
    return TestClient(app)


@pytest.fixture
def client_with_runner(mock_claude_cli):
    """Client with runner connector wired up (offline state)."""
    app = create_app()
    app.state.tracker = TokenTracker()
    runner_client = RunnerClient(base_url="http://localhost:17777", token="")
    runner_poller = RunnerPoller(client=runner_client)
    runner_poller.state = RunnerState()  # default = offline
    app.state.runner_client = runner_client
    app.state.runner_poller = runner_poller
    return TestClient(app)


class TestAPIRoutes:
    def test_list_agents(self, client):
        resp = client.get("/api/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["agents"]) == 4
        agent_ids = [a["id"] for a in data["agents"]]
        assert "manager" in agent_ids
        assert "frontend" in agent_ids
        assert "backend" in agent_ids
        assert "tester" in agent_ids

    def test_system_status(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "token_usage" in data

    def test_reset_budget(self, client):
        resp = client.post("/api/budget/reset")
        assert resp.status_code == 200
        assert resp.json()["status"] == "budget reset"

    def test_chat_with_agent(self, client):
        resp = client.post("/api/chat", json={
            "agent": "frontend",
            "message": "Hello",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "frontend"
        assert "response" in data

    def test_chat_unknown_agent(self, client):
        resp = client.post("/api/chat", json={
            "agent": "nonexistent",
            "message": "Hello",
        })
        assert resp.status_code == 400

    def test_budget_exhausted_returns_429(self, client):
        client.app.state.tracker._daily_limit = 0
        client.app.state.tracker._call_count = 1
        resp = client.post("/api/chat", json={
            "agent": "frontend",
            "message": "Hello",
        })
        assert resp.status_code == 429


class TestRunnerEndpoints:
    """Smoke tests for runner proxy endpoints."""

    def test_runner_status_returns_offline_when_not_started(self, client_with_runner):
        """Runner status should report offline when runner is unreachable."""
        resp = client_with_runner.get("/api/runner/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["online"] is False
        assert "last_seen" in data

    def test_runner_cmd_returns_503_when_offline(self, client_with_runner):
        """Command execution should return 503 when runner is offline."""
        resp = client_with_runner.post("/api/runner/cmd", json={
            "projectId": "test",
            "command": "echo hello",
        })
        assert resp.status_code == 503

    def test_runner_git_status_returns_503_when_offline(self, client_with_runner):
        """Git status should return 503 when runner is offline."""
        resp = client_with_runner.get("/api/runner/git/status?projectId=test")
        assert resp.status_code == 503

    def test_runner_dev_state_returns_offline_gracefully(self, client_with_runner):
        """Dev state should return offline status instead of erroring."""
        resp = client_with_runner.get("/api/runner/dev-state/test-project")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "offline"
        assert data["project_id"] == "test-project"
