"""Tests for the LangGraph orchestrator."""

from __future__ import annotations

from src.orchestrator.state import SubtaskResult, WorkflowState
from src.orchestrator.graph import intake_node, route_after_review


def _make_state(**overrides) -> dict:
    """Create a WorkflowState-shaped dict with defaults."""
    defaults = {
        "messages": [],
        "task": "",
        "plan": "",
        "subtasks": [],
        "results": {},
        "phase": "intake",
        "ready_queue": [],
        "final_output": "",
        "errors": [],
        "max_revisions": 2,
    }
    defaults.update(overrides)
    return defaults


class TestWorkflowState:
    def test_state_dict_defaults(self):
        state = _make_state()
        assert state["phase"] == "intake"
        assert state["task"] == ""
        assert state["subtasks"] == []
        assert state["results"] == {}

    def test_subtask_result(self):
        result = SubtaskResult(
            subtask_id="st-1",
            agent_type="frontend",
            description="Build a navbar",
            output="<nav>...</nav>",
            review_verdict="approve",
            review_score=9,
        )
        assert result.subtask_id == "st-1"
        assert result.review_verdict == "approve"


class TestIntakeNode:
    def test_transitions_to_planning(self):
        state = _make_state(task="Build a landing page")
        result = intake_node(state)
        assert result["phase"] == "planning"


class TestRouting:
    def test_route_to_synthesize(self):
        state = _make_state(phase="synthesizing")
        assert route_after_review(state) == "synthesize"

    def test_route_to_execute(self):
        state = _make_state(phase="executing")
        assert route_after_review(state) == "execute"


class TestSubtaskResult:
    def test_default_pending(self):
        r = SubtaskResult(subtask_id="1", agent_type="backend", description="test")
        assert r.review_verdict == "pending"
        assert r.attempts == 0
        assert r.output == ""

    def test_increment_attempts(self):
        r = SubtaskResult(subtask_id="1", agent_type="backend", description="test")
        r.attempts += 1
        assert r.attempts == 1
