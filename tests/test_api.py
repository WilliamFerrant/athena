"""Tests for the FastAPI routes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_app
from src.token_tracker.tracker import TokenTracker


@pytest.fixture
def client(mock_claude_cli):
    app = create_app()
    app.state.tracker = TokenTracker()
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
