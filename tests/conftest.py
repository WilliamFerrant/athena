"""Shared test fixtures."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.token_tracker.tracker import TokenTracker, UsageRecord, ClaudeResponse
from src.token_tracker.session_parser import UsageReport


@pytest.fixture
def mock_claude_cli():
    """Patch subprocess.run to avoid calling real Claude CLI."""
    with patch("src.token_tracker.tracker.subprocess") as mock_sp:
        result = MagicMock()
        result.stdout = "Hello from Claude"
        result.stderr = ""
        result.returncode = 0
        mock_sp.run.return_value = result
        yield mock_sp


@pytest.fixture
def mock_session_parser():
    """Patch parse_all_sessions to avoid reading real ~/.claude files."""
    empty_report = UsageReport(totals={
        "total_sessions": 0,
        "total_queries": 0,
        "total_tokens": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    })
    with patch("src.token_tracker.tracker.parse_all_sessions", return_value=empty_report) as mock_parse:
        yield mock_parse


@pytest.fixture
def tracker(mock_claude_cli, mock_session_parser) -> TokenTracker:
    """A TokenTracker with mocked CLI and session parser."""
    return TokenTracker()


@pytest.fixture
def fake_response() -> ClaudeResponse:
    return ClaudeResponse(text="Hello from Claude", input_chars=100)
