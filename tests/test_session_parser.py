"""Tests for the session parser — real Claude Code token tracking."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.token_tracker.session_parser import (
    QueryRecord,
    SessionData,
    UsageReport,
    _extract_session_data,
    _fmt,
    _parse_jsonl_file,
    parse_all_sessions,
    report_to_dict,
)


# -- Fixtures ------------------------------------------------------------------


@pytest.fixture
def tmp_claude_dir(tmp_path: Path) -> Path:
    """Create a fake ~/.claude directory structure with test session data."""
    claude_dir = tmp_path / ".claude"
    projects_dir = claude_dir / "projects" / "test-project"
    projects_dir.mkdir(parents=True)

    # Create a session JSONL file with realistic entries
    session_id = "test-session-001"
    entries = [
        # User message
        {
            "type": "user",
            "message": {"role": "user", "content": "Build a landing page"},
            "timestamp": "2026-02-19T10:00:00.000Z",
            "sessionId": session_id,
        },
        # Assistant response with usage
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "I'll build that for you."}],
                "usage": {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 500,
                    "cache_read_input_tokens": 2000,
                    "output_tokens": 300,
                },
            },
            "timestamp": "2026-02-19T10:00:05.000Z",
            "sessionId": session_id,
        },
        # Another user message
        {
            "type": "user",
            "message": {"role": "user", "content": "Add a contact form"},
            "timestamp": "2026-02-19T10:01:00.000Z",
            "sessionId": session_id,
        },
        # Another assistant response
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "Done, here's the form."}],
                "usage": {
                    "input_tokens": 200,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 3000,
                    "output_tokens": 500,
                },
            },
            "timestamp": "2026-02-19T10:01:10.000Z",
            "sessionId": session_id,
        },
    ]

    jsonl_path = projects_dir / f"{session_id}.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    # Create a second session with a different model
    session_id2 = "test-session-002"
    entries2 = [
        {
            "type": "user",
            "message": {"role": "user", "content": "Review my code"},
            "timestamp": "2026-02-18T14:00:00.000Z",
            "sessionId": session_id2,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": [{"type": "text", "text": "LGTM"}],
                "usage": {
                    "input_tokens": 50,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 1000,
                    "output_tokens": 100,
                },
            },
            "timestamp": "2026-02-18T14:00:03.000Z",
            "sessionId": session_id2,
        },
    ]

    jsonl_path2 = projects_dir / f"{session_id2}.jsonl"
    with open(jsonl_path2, "w", encoding="utf-8") as f:
        for entry in entries2:
            f.write(json.dumps(entry) + "\n")

    # Create history.jsonl
    history = [
        {
            "display": "Build a landing page",
            "sessionId": session_id,
            "timestamp": 1771512000000,
            "project": "test-project",
        },
        {
            "display": "Review my code",
            "sessionId": session_id2,
            "timestamp": 1771425600000,
            "project": "test-project",
        },
    ]
    history_path = claude_dir / "history.jsonl"
    with open(history_path, "w", encoding="utf-8") as f:
        for entry in history:
            f.write(json.dumps(entry) + "\n")

    return claude_dir


# -- Unit tests ----------------------------------------------------------------


class TestFmt:
    def test_millions(self):
        assert _fmt(1_500_000) == "1.5M"

    def test_ten_thousands(self):
        assert _fmt(25_000) == "25K"

    def test_thousands(self):
        assert _fmt(1_500) == "1.5K"

    def test_small(self):
        assert _fmt(42) == "42"


class TestParseJsonlFile:
    def test_valid_file(self, tmp_path: Path):
        p = tmp_path / "test.jsonl"
        p.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
        result = _parse_jsonl_file(p)
        assert len(result) == 2
        assert result[0] == {"a": 1}

    def test_skips_malformed_lines(self, tmp_path: Path):
        p = tmp_path / "test.jsonl"
        p.write_text('{"a": 1}\nnot json\n{"b": 2}\n', encoding="utf-8")
        result = _parse_jsonl_file(p)
        assert len(result) == 2

    def test_missing_file(self, tmp_path: Path):
        result = _parse_jsonl_file(tmp_path / "nonexistent.jsonl")
        assert result == []

    def test_empty_lines_skipped(self, tmp_path: Path):
        p = tmp_path / "test.jsonl"
        p.write_text('\n{"a": 1}\n\n\n{"b": 2}\n\n', encoding="utf-8")
        result = _parse_jsonl_file(p)
        assert len(result) == 2


class TestExtractSessionData:
    def test_extracts_queries(self):
        entries = [
            {
                "type": "user",
                "message": {"role": "user", "content": "Hello"},
                "timestamp": "2026-02-19T10:00:00Z",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 100,
                        "cache_creation_input_tokens": 50,
                        "cache_read_input_tokens": 500,
                        "output_tokens": 200,
                    },
                },
                "timestamp": "2026-02-19T10:00:05Z",
            },
        ]
        queries = _extract_session_data(entries)
        assert len(queries) == 1
        q = queries[0]
        assert q.model == "claude-sonnet-4-6"
        assert q.input_tokens == 650  # 100 + 50 + 500
        assert q.output_tokens == 200
        assert q.total_tokens == 850
        assert q.user_prompt == "Hello"

    def test_skips_meta_messages(self):
        entries = [
            {
                "type": "user",
                "message": {"role": "user", "content": "meta stuff"},
                "isMeta": True,
                "timestamp": "2026-02-19T10:00:00Z",
            },
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
                "timestamp": "2026-02-19T10:00:05Z",
            },
        ]
        queries = _extract_session_data(entries)
        assert len(queries) == 1
        # user_prompt should be None because the user message was meta
        assert queries[0].user_prompt is None

    def test_skips_command_messages(self):
        entries = [
            {
                "type": "user",
                "message": {"role": "user", "content": "<command-name>/init</command-name>"},
                "timestamp": "2026-02-19T10:00:00Z",
            },
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
                "timestamp": "2026-02-19T10:00:05Z",
            },
        ]
        queries = _extract_session_data(entries)
        assert len(queries) == 1
        assert queries[0].user_prompt is None

    def test_skips_synthetic_model(self):
        entries = [
            {
                "type": "assistant",
                "message": {
                    "model": "<synthetic>",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            },
        ]
        queries = _extract_session_data(entries)
        assert len(queries) == 0


class TestParseAllSessions:
    def test_parses_test_data(self, tmp_claude_dir: Path):
        report = parse_all_sessions(claude_dir=tmp_claude_dir)
        assert isinstance(report, UsageReport)
        assert len(report.sessions) == 2
        assert report.totals["total_sessions"] == 2
        assert report.totals["total_queries"] == 3  # 2 + 1
        assert report.totals["total_tokens"] > 0

    def test_daily_usage(self, tmp_claude_dir: Path):
        report = parse_all_sessions(claude_dir=tmp_claude_dir)
        assert len(report.daily_usage) == 2  # Two different dates
        dates = [d.date for d in report.daily_usage]
        assert "2026-02-19" in dates
        assert "2026-02-18" in dates

    def test_model_breakdown(self, tmp_claude_dir: Path):
        report = parse_all_sessions(claude_dir=tmp_claude_dir)
        models = {m.model for m in report.model_breakdown}
        assert "claude-sonnet-4-6" in models
        assert "claude-opus-4-6" in models

    def test_token_counts_correct(self, tmp_claude_dir: Path):
        report = parse_all_sessions(claude_dir=tmp_claude_dir)
        # Session 1: query1 input=100+500+2000=2600, out=300; query2 input=200+100+3000=3300, out=500
        # Session 2: query1 input=50+0+1000=1050, out=100
        assert report.totals["total_input_tokens"] == 2600 + 3300 + 1050
        assert report.totals["total_output_tokens"] == 300 + 500 + 100

    def test_sessions_sorted_by_tokens(self, tmp_claude_dir: Path):
        report = parse_all_sessions(claude_dir=tmp_claude_dir)
        tokens = [s.total_tokens for s in report.sessions]
        assert tokens == sorted(tokens, reverse=True)

    def test_empty_projects_dir(self, tmp_path: Path):
        claude_dir = tmp_path / ".claude"
        # No projects dir at all
        report = parse_all_sessions(claude_dir=claude_dir)
        assert report.sessions == []
        assert report.totals == {}

    def test_top_prompts(self, tmp_claude_dir: Path):
        report = parse_all_sessions(claude_dir=tmp_claude_dir)
        assert len(report.top_prompts) > 0
        # Should be sorted descending by total_tokens
        tokens = [p.total_tokens for p in report.top_prompts]
        assert tokens == sorted(tokens, reverse=True)


class TestReportToDict:
    def test_serializable(self, tmp_claude_dir: Path):
        report = parse_all_sessions(claude_dir=tmp_claude_dir)
        d = report_to_dict(report)
        # Should be JSON-serializable
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert "sessions" in parsed
        assert "daily_usage" in parsed
        assert "model_breakdown" in parsed
        assert "top_prompts" in parsed
        assert "totals" in parsed
        assert "insights" in parsed

    def test_session_fields(self, tmp_claude_dir: Path):
        report = parse_all_sessions(claude_dir=tmp_claude_dir)
        d = report_to_dict(report)
        session = d["sessions"][0]
        assert "session_id" in session
        assert "project" in session
        assert "input_tokens" in session
        assert "output_tokens" in session
        assert "total_tokens" in session
        assert "model" in session
