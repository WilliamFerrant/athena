"""Tests for the token tracker module (Claude CLI backend)."""

from __future__ import annotations

from src.token_tracker.tracker import TokenTracker, UsageRecord


class TestUsageRecord:
    def test_creation(self):
        record = UsageRecord(
            agent_id="test-agent",
            model="sonnet",
            input_chars=500,
            output_chars=200,
        )
        assert record.agent_id == "test-agent"
        assert record.input_chars == 500
        assert record.output_chars == 200
        assert record.cost_usd == 0.0  # Always free with CLI

    def test_default_timestamp(self):
        record = UsageRecord(agent_id="test", model="test", input_chars=0, output_chars=0)
        assert record.timestamp > 0


class TestTokenTracker:
    def test_create_message_tracks_usage(self, tracker):
        response = tracker.create_message(
            agent_id="test-agent",
            messages=[{"role": "user", "content": "hello"}],
        )
        assert len(tracker.records) == 1
        assert tracker.records[0].agent_id == "test-agent"
        assert response.text == "Hello from Claude"

    def test_agent_summary(self, tracker):
        tracker.create_message(
            agent_id="frontend",
            messages=[{"role": "user", "content": "a"}],
        )
        tracker.create_message(
            agent_id="frontend",
            messages=[{"role": "user", "content": "b"}],
        )
        tracker.create_message(
            agent_id="backend",
            messages=[{"role": "user", "content": "c"}],
        )

        summary = tracker.agent_summary("frontend")
        assert summary["calls"] == 2

        all_summaries = tracker.all_agents_summary()
        assert len(all_summaries) == 2

    def test_global_summary(self, tracker):
        tracker.create_message(
            agent_id="test",
            messages=[{"role": "user", "content": "hi"}],
        )
        summary = tracker.global_summary()
        assert summary["total_calls"] == 1
        assert summary["total_cost_usd"] == 0.0
        assert "daily_call_limit" in summary
        assert "real_usage" in summary

    def test_call_limit_enforcement(self, tracker):
        tracker._daily_limit = 1
        tracker._call_count = 1

        import pytest

        with pytest.raises(RuntimeError, match="call limit"):
            tracker.create_message(
                agent_id="test",
                messages=[{"role": "user", "content": "hi"}],
            )

    def test_reset_daily(self, tracker):
        tracker._call_count = 50
        tracker.reset_daily()
        assert tracker._call_count == 0

    def test_calls_remaining(self, tracker):
        tracker._daily_limit = 200
        tracker._call_count = 30
        assert tracker.budget_remaining == 170

    def test_total_cost_always_zero(self, tracker):
        tracker.create_message(
            agent_id="test",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert tracker.total_cost == 0.0

    def test_build_prompt_with_system(self):
        prompt = TokenTracker._build_prompt(
            system="You are helpful",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert "[System]" in prompt
        assert "You are helpful" in prompt
        assert "[User]" in prompt

    def test_build_prompt_without_system(self):
        prompt = TokenTracker._build_prompt(
            system=None,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert "[System]" not in prompt
        assert "[User]" in prompt
