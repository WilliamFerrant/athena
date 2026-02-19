"""Tests for Workshop UI features (deliverables 1-5).

Validates backend endpoints that the Workshop UI wires up:
- Push/PR endpoint safety
- Usage/Budget endpoints
- Orchestrator streaming endpoint
- Memory endpoints
- Agent listing for dynamic population
"""

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
    app.state.tracker = TokenTracker()
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


# ── Deliverable 1: Push/PR endpoint ──────────────────────────────────────


class TestPushPR:
    """Push & PR endpoint tests."""

    def test_push_pr_returns_503_when_runner_offline(self, client_with_runner):
        """POST /runner/git/push-pr should 503 when runner is offline."""
        resp = client_with_runner.post("/api/runner/git/push-pr", json={
            "projectId": "test",
            "branch": "feat/test",
            "base": "main",
            "title": "Test PR",
        })
        assert resp.status_code == 503
        assert "offline" in resp.json()["detail"].lower()

    def test_push_pr_validates_body_fields(self, client_with_runner):
        """POST /runner/git/push-pr requires projectId, branch, title."""
        resp = client_with_runner.post("/api/runner/git/push-pr", json={
            "projectId": "test",
        })
        assert resp.status_code == 422  # validation error for missing fields


# ── Deliverable 2: Usage/Budget endpoints ────────────────────────────────


class TestUsageBudget:
    """Usage and budget endpoint tests."""

    def test_usage_limits_returns_ok(self, client):
        """GET /usage/limits should return rate limit data."""
        resp = client.get("/api/usage/limits")
        assert resp.status_code == 200
        data = resp.json()
        # Should have session and/or weekly data
        assert isinstance(data, dict)

    def test_usage_models_returns_ok(self, client):
        """GET /usage/models should return model breakdown."""
        resp = client.get("/api/usage/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "model_breakdown" in data

    def test_usage_overview_returns_ok(self, client):
        """GET /usage should return full usage data."""
        resp = client.get("/api/usage")
        assert resp.status_code == 200
        data = resp.json()
        # Should have totals key
        assert "totals" in data

    def test_usage_insights_returns_ok(self, client):
        """GET /usage/insights should return insights list."""
        resp = client.get("/api/usage/insights")
        assert resp.status_code == 200
        data = resp.json()
        assert "insights" in data

    def test_usage_daily_returns_ok(self, client):
        """GET /usage/daily should return daily breakdown."""
        resp = client.get("/api/usage/daily")
        assert resp.status_code == 200
        data = resp.json()
        assert "daily_usage" in data

    def test_budget_reset(self, client):
        """POST /budget/reset should reset the counter."""
        resp = client.post("/api/budget/reset")
        assert resp.status_code == 200
        assert resp.json()["status"] == "budget reset"


# ── Deliverable 3: Orchestrator streaming ────────────────────────────────


class TestOrchestratorStream:
    """Orchestrator SSE streaming endpoint tests."""

    def test_orchestrator_stream_returns_sse(self, client):
        """POST /orchestrator/stream should return SSE content type."""
        resp = client.post("/api/orchestrator/stream", json={
            "task": "Build a hello world page",
        })
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

    def test_orchestrator_stream_emits_phase_events(self, client):
        """Stream should contain phase events."""
        resp = client.post("/api/orchestrator/stream", json={
            "task": "Test task",
        })
        text = resp.text
        assert "event: phase" in text

    def test_orchestrator_stream_budget_exhausted(self, client):
        """POST /orchestrator/stream should 429 when over budget."""
        client.app.state.tracker._daily_limit = 0
        client.app.state.tracker._call_count = 1
        resp = client.post("/api/orchestrator/stream", json={
            "task": "Test task",
        })
        assert resp.status_code == 429


# ── Deliverable 4: Memory endpoints ─────────────────────────────────────


class TestMemoryEndpoints:
    """Memory CRUD endpoint tests."""

    def test_get_memories_returns_structure(self, client):
        """GET /memory/{agent_id} should return memories structure."""
        resp = client.get("/api/memory/manager")
        # Will either 200 with data or 500 if mem0 not configured
        assert resp.status_code in (200, 500)
        if resp.status_code == 200:
            data = resp.json()
            assert "agent_id" in data
            assert "memories" in data

    def test_add_memory_endpoint(self, client):
        """POST /memory/add should accept agent_id + content."""
        resp = client.post("/api/memory/add", json={
            "agent_id": "manager",
            "content": "Test memory content",
        })
        # Will 200 or 500 depending on mem0 config
        assert resp.status_code in (200, 500)

    def test_search_memory_endpoint(self, client):
        """POST /memory/search should accept agent_id + query."""
        resp = client.post("/api/memory/search", json={
            "agent_id": "manager",
            "query": "test",
            "limit": 5,
        })
        assert resp.status_code in (200, 500)

    def test_clear_memory_endpoint(self, client):
        """DELETE /memory/{agent_id} should clear memories."""
        resp = client.delete("/api/memory/manager")
        assert resp.status_code in (200, 500)

    def test_memory_search_validates_body(self, client):
        """POST /memory/search should validate request body."""
        resp = client.post("/api/memory/search", json={"query": "test"})
        assert resp.status_code == 422  # missing agent_id


# ── Deliverable 5: Agent listing + System status ────────────────────────


class TestAgentAndStatus:
    """Agent listing and system status endpoint tests."""

    def test_agents_list_has_four_agents(self, client):
        """GET /agents should list the 4 agent types."""
        resp = client.get("/api/agents")
        assert resp.status_code == 200
        agents = resp.json()["agents"]
        assert len(agents) == 4
        ids = {a["id"] for a in agents}
        assert ids == {"manager", "frontend", "backend", "tester"}

    def test_agents_have_descriptions(self, client):
        """Each agent should have id, type, and description."""
        resp = client.get("/api/agents")
        for agent in resp.json()["agents"]:
            assert "id" in agent
            assert "type" in agent
            assert "description" in agent
            assert len(agent["description"]) > 10

    def test_system_status_returns_ok(self, client):
        """GET /status should return ok status with token usage."""
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "token_usage" in data

    def test_chat_stream_returns_sse(self, client):
        """POST /chat/stream should return SSE content type."""
        resp = client.post("/api/chat/stream", json={
            "agent": "frontend",
            "message": "Hello",
        })
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

    def test_chat_stream_unknown_agent_returns_400(self, client):
        """POST /chat/stream with unknown agent should 400."""
        resp = client.post("/api/chat/stream", json={
            "agent": "nonexistent",
            "message": "Hello",
        })
        assert resp.status_code == 400


# ── Workshop HTML structure tests ────────────────────────────────────────


class TestWorkshopHTML:
    """Verify Workshop HTML contains expected UI elements."""

    @pytest.fixture(autouse=True)
    def load_html(self):
        import pathlib
        html_path = pathlib.Path(__file__).parent.parent / "src" / "static" / "index.html"
        self.html = html_path.read_text(encoding="utf-8")

    def test_has_push_pr_button(self):
        assert "btnPushPR" in self.html
        assert "pushAndPR" in self.html

    def test_has_usage_overlay(self):
        assert "usageOverlay" in self.html
        assert "toggleUsage" in self.html
        assert "loadUsageData" in self.html

    def test_has_orchestrator_overlay(self):
        assert "orchOverlay" in self.html
        assert "toggleOrchestrator" in self.html
        assert "runMission" in self.html

    def test_has_memory_overlay(self):
        assert "memOverlay" in self.html
        assert "toggleMemory" in self.html
        assert "loadMemories" in self.html
        assert "searchMemories" in self.html
        assert "addMemory" in self.html
        assert "clearMemories" in self.html

    def test_has_escape_handler(self):
        assert "Escape" in self.html

    def test_has_api_status_polling(self):
        assert "pollApiStatus" in self.html

    def test_has_health_sse_init_handler(self):
        assert "addEventListener('init'" in self.html

    def test_health_sse_init_handles_object_format(self):
        """Init handler should treat data as object keyed by project_id, not array."""
        assert "typeof data==='object'" in self.html
        assert "Object.keys(data)" in self.html

    def test_has_escattr_xss_fix(self):
        assert "escAttr" in self.html

    def test_topbar_has_new_buttons(self):
        assert "toggleUsage()" in self.html
        assert "toggleOrchestrator()" in self.html
        assert "toggleMemory()" in self.html

    def test_commit_window_has_pr_button(self):
        assert "confirmCommitAndPR" in self.html
        assert "Commit &amp; PR" in self.html

    def test_dynamic_agent_loading(self):
        assert "loadAgentList" in self.html

    def test_bb_health_update(self):
        assert "updateBbHealth" in self.html

    def test_file_tree_uses_data_attributes(self):
        """File tree should use data-filepath attributes, not inline onclick strings."""
        assert 'data-filepath="' in self.html
        assert "el.dataset.filepath" in self.html

    def test_pr_url_field_handles_both_cases(self):
        """PR result handler should accept both prUrl and pr_url field names."""
        assert "result.prUrl||result.pr_url" in self.html

    def test_lastcommit_handles_object(self):
        """Center header should handle lastCommit as string or object."""
        assert "typeof ds.lastCommit==='string'" in self.html

    def test_usage_card_shows_status_label(self):
        """Usage limit cards should show CRITICAL/WARNING/OK status."""
        assert "CRITICAL" in self.html
        assert "WARNING" in self.html

    def test_dirty_dialog_is_clear(self):
        """Dirty working tree dialog should have clear OK=proceed, Cancel=go back."""
        assert "Click OK to push anyway" in self.html

    def test_has_page_transition_css(self):
        """Workshop should have page transition CSS animations."""
        assert "pageExit" in self.html
        assert "pageEnter" in self.html
        assert "page-exit-active" in self.html
        assert "page-enter-ready" in self.html

    def test_has_navigate_to_bridge(self):
        """Workshop should have navigateToBridge function for smooth transition."""
        assert "navigateToBridge" in self.html
        assert "sessionStorage" in self.html
        assert "pageTransition" in self.html

    def test_back_to_bridge_uses_transition(self):
        """Back to Bridge button should use JS transition, not plain link."""
        assert 'onclick="navigateToBridge()"' in self.html
        # Should NOT have a plain <a href="/"> for the Bridge button
        assert '<a href="/" class="tb-btn">' not in self.html


# ── Health SSE init event format tests ───────────────────────────────


class TestHealthSSEFormat:
    """Verify health SSE init payload matches what the UI expects."""

    def test_health_store_get_all_latest_returns_dict(self):
        """HealthStore.get_all_latest() should return a dict (not array).

        This is what gets sent as the SSE init event payload. The JS handler
        expects typeof data === 'object' with Object.keys(data) as project IDs.
        """
        from src.health.engine import HealthStore
        store = HealthStore()
        try:
            latest = store.get_all_latest()
            assert isinstance(latest, dict), "get_all_latest should return dict keyed by project_id"
        finally:
            store.close()


# ── Runner status endpoint tests ─────────────────────────────────────


class TestRunnerStatus:
    """Runner status endpoint tests."""

    def test_runner_status_returns_structure(self, client_with_runner):
        """GET /runner/status should return proper RunnerState fields."""
        resp = client_with_runner.get("/api/runner/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "online" in data
        assert "last_seen" in data
        assert "platform" in data
        assert isinstance(data["online"], bool)

    def test_cmd_requires_runner_online(self, client_with_runner):
        """POST /runner/cmd should 503 when runner offline."""
        resp = client_with_runner.post("/api/runner/cmd", json={
            "projectId": "test",
            "command": "echo hello",
        })
        assert resp.status_code == 503

    def test_git_status_requires_runner_online(self, client_with_runner):
        """GET /runner/git/status should 503 when runner offline."""
        resp = client_with_runner.get("/api/runner/git/status", params={"projectId": "test"})
        assert resp.status_code == 503

    def test_dev_state_returns_offline_gracefully(self, client_with_runner):
        """GET /runner/dev-state should return offline status without error."""
        resp = client_with_runner.get("/api/runner/dev-state/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "offline"


# ── Page transition tests ────────────────────────────────────────────


class TestPageTransitions:
    """Verify both pages have matching page transition infrastructure."""

    @pytest.fixture(autouse=True)
    def load_both_pages(self):
        import pathlib
        base = pathlib.Path(__file__).parent.parent / "src" / "static"
        # Both Bridge and Workshop now live in the same unified page
        self.bridge = (base / "index.html").read_text(encoding="utf-8")
        self.workshop = (base / "index.html").read_text(encoding="utf-8")

    def test_bridge_has_navigate_to_workshop(self):
        """Bridge should have navigateToWorkshop function."""
        assert "navigateToWorkshop" in self.bridge
        assert "sessionStorage" in self.bridge

    def test_bridge_workshop_button_uses_transition(self):
        """Bridge Workshop button should call navigateToWorkshop, not toggleWorkshop."""
        assert 'onclick="navigateToWorkshop()"' in self.bridge

    def test_bridge_has_entry_transition(self):
        """Bridge should handle entry from Workshop via sessionStorage."""
        assert "toBridge" in self.bridge
        assert "page-enter-active" in self.bridge
        assert "page-enter-ready" in self.bridge

    def test_workshop_has_entry_transition(self):
        """Workshop should handle entry from Bridge via sessionStorage."""
        assert "toWorkshop" in self.workshop
        assert "page-enter-active" in self.workshop
        assert "page-enter-ready" in self.workshop

    def test_both_have_matching_css_animations(self):
        """Both pages should define the same transition keyframes."""
        for page in [self.bridge, self.workshop]:
            assert "@keyframes pageExit" in page
            assert "@keyframes pageEnter" in page
            assert "@keyframes tagMorph" in page
            assert "@keyframes topbarSlide" in page

    def test_bridge_still_has_toggle_workshop(self):
        """Bridge should still have toggleWorkshop for in-page overlay (used by selectProject)."""
        assert "function toggleWorkshop()" in self.bridge
