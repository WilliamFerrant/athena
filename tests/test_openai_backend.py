"""Tests for the OpenAI ChatGPT backend."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from src.token_tracker.tracker import ClaudeResponse, UsageRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_openai():
    """Patch the openai module so no real API calls are made."""
    with patch.dict("sys.modules", {"openai": MagicMock()}):
        import openai

        # Mock sync client
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Hello from ChatGPT"
        mock_client.chat.completions.create.return_value = mock_response

        # Mock async client
        mock_async_client = AsyncMock()
        mock_async_response = MagicMock()
        mock_async_response.choices = [MagicMock()]
        mock_async_response.choices[0].message.content = "Hello from ChatGPT async"
        mock_async_client.chat.completions.create.return_value = mock_async_response

        openai.OpenAI.return_value = mock_client
        openai.AsyncOpenAI.return_value = mock_async_client

        yield {
            "openai": openai,
            "client": mock_client,
            "async_client": mock_async_client,
            "response": mock_response,
            "async_response": mock_async_response,
        }


@pytest.fixture
def openai_backend(mock_openai):
    """An OpenAIBackend with fully mocked OpenAI client."""
    from src.token_tracker.openai_backend import OpenAIBackend

    backend = OpenAIBackend(api_key="sk-test-key", model="gpt-4o")
    return backend


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOpenAIBackend:
    def test_create_message_returns_claude_response(self, openai_backend):
        response = openai_backend.create_message(
            agent_id="manager",
            system="You are a manager.",
            messages=[{"role": "user", "content": "Plan a landing page"}],
        )
        assert isinstance(response, ClaudeResponse)
        assert response.content[0].type == "text"
        assert "ChatGPT" in response.content[0].text

    def test_tracks_usage(self, openai_backend):
        openai_backend.create_message(
            agent_id="manager",
            messages=[{"role": "user", "content": "hello"}],
        )
        assert len(openai_backend.records) == 1
        assert openai_backend.records[0].agent_id == "manager"
        assert openai_backend.records[0].model == "gpt-4o"

    def test_agent_summary(self, openai_backend):
        openai_backend.create_message(
            agent_id="manager",
            messages=[{"role": "user", "content": "test"}],
        )
        summary = openai_backend.agent_summary("manager")
        assert summary["agent_id"] == "manager"
        assert summary["backend"] == "openai"
        assert summary["calls"] == 1

    def test_budget_tracking(self, openai_backend):
        assert openai_backend.budget_remaining > 0
        assert not openai_backend.is_over_budget

    def test_build_messages_with_system(self, openai_backend):
        msgs = openai_backend._build_messages(
            system="Be helpful",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert msgs[0] == {"role": "system", "content": "Be helpful"}
        assert msgs[1] == {"role": "user", "content": "hi"}

    def test_build_messages_without_system(self, openai_backend):
        msgs = openai_backend._build_messages(
            system=None,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_model_override(self, openai_backend, mock_openai):
        openai_backend.create_message(
            agent_id="manager",
            model="gpt-4-turbo",
            messages=[{"role": "user", "content": "test"}],
        )
        # Record should show the overridden model
        assert openai_backend.records[0].model == "gpt-4-turbo"


class TestManagerWithOpenAI:
    """Test that ManagerAgent picks up OpenAI backend when configured."""

    def test_manager_uses_openai_when_key_set(self, mock_openai, tracker):
        """Manager should auto-detect the OpenAI backend."""
        with patch("src.agents.manager.settings") as mock_settings:
            mock_settings.openai_api_key = "sk-test"
            mock_settings.openai_model = "gpt-4o"
            mock_settings.manager_backend = "auto"
            mock_settings.manager_model = "sonnet"

            from src.agents.manager import ManagerAgent

            agent = ManagerAgent(agent_id="mgr", tracker=tracker)
            assert agent._llm_backend is not None
            assert agent.default_model == "gpt-4o"

    def test_manager_falls_back_to_claude(self, tracker):
        """Manager should use Claude CLI when no OpenAI key."""
        with patch("src.agents.manager.settings") as mock_settings:
            mock_settings.openai_api_key = ""
            mock_settings.openai_model = "gpt-4o"
            mock_settings.manager_backend = "auto"
            mock_settings.manager_model = "sonnet"

            from src.agents.manager import ManagerAgent

            agent = ManagerAgent(agent_id="mgr", tracker=tracker)
            assert agent._llm_backend is None

    def test_manager_forced_claude(self, mock_openai, tracker):
        """manager_backend='claude' forces Claude CLI even with key."""
        with patch("src.agents.manager.settings") as mock_settings:
            mock_settings.openai_api_key = "sk-test"
            mock_settings.openai_model = "gpt-4o"
            mock_settings.manager_backend = "claude"
            mock_settings.manager_model = "sonnet"

            from src.agents.manager import ManagerAgent

            agent = ManagerAgent(agent_id="mgr", tracker=tracker)
            assert agent._llm_backend is None
