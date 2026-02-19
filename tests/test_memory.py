"""Tests for the mem0 memory layer."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.memory.mem0_client import AgentMemory


@pytest.fixture
def mock_mem0():
    with patch("src.memory.mem0_client.MemoryClient") as mock_cls:
        client = MagicMock()
        mock_cls.return_value = client
        yield client


@pytest.fixture
def memory(mock_mem0) -> AgentMemory:
    return AgentMemory(agent_id="test-agent", api_key="m0-test-fake", window=5)


class TestAgentMemory:
    def test_add(self, memory, mock_mem0):
        mock_mem0.add.return_value = {"id": "mem-1"}
        mock_mem0.get_all.return_value = {"results": []}

        result = memory.add("I learned something")
        assert result["id"] == "mem-1"
        mock_mem0.add.assert_called_once()

    def test_search(self, memory, mock_mem0):
        mock_mem0.search.return_value = {
            "results": [
                {"memory": "React is good", "score": 0.95},
                {"memory": "Use TypeScript", "score": 0.85},
            ]
        }
        results = memory.search("frontend tech")
        assert len(results) == 2

    def test_get_all(self, memory, mock_mem0):
        mock_mem0.get_all.return_value = {
            "results": [{"id": "1"}, {"id": "2"}]
        }
        results = memory.get_all()
        assert len(results) == 2

    def test_get_relevant_context(self, memory, mock_mem0):
        mock_mem0.search.return_value = {
            "results": [
                {"memory": "Use FastAPI for backend"},
                {"memory": "PostgreSQL for persistence"},
            ]
        }
        ctx = memory.get_relevant_context("building a backend API")
        assert "Relevant memories" in ctx
        assert "FastAPI" in ctx

    def test_get_relevant_context_empty(self, memory, mock_mem0):
        mock_mem0.search.return_value = {"results": []}
        ctx = memory.get_relevant_context("something obscure")
        assert ctx == ""

    def test_prune_when_over_window(self, memory, mock_mem0):
        # Window is 5; create 7 memories
        mems = [
            {"id": str(i), "created_at": f"2025-01-0{i+1}"}
            for i in range(7)
        ]
        mock_mem0.get_all.return_value = {"results": mems}
        mock_mem0.add.return_value = {"id": "new"}

        memory.add("new memory")

        # Should have deleted the 2 oldest
        assert mock_mem0.delete.call_count == 2
        deleted_ids = [call.args[0] for call in mock_mem0.delete.call_args_list]
        assert "0" in deleted_ids
        assert "1" in deleted_ids

    def test_clear(self, memory, mock_mem0):
        memory.clear()
        mock_mem0.delete_all.assert_called_once_with(user_id="test-agent")

    def test_stats(self, memory, mock_mem0):
        mock_mem0.get_all.return_value = {"results": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}
        stats = memory.stats()
        assert stats["agent_id"] == "test-agent"
        assert stats["memory_count"] == 3
        assert stats["window"] == 5

    def test_add_conversation(self, memory, mock_mem0):
        mock_mem0.add.return_value = {"id": "conv-1"}
        mock_mem0.get_all.return_value = {"results": []}

        messages = [
            {"role": "user", "content": "How do I use hooks?"},
            {"role": "assistant", "content": "React hooks let you..."},
        ]
        memory.add_conversation(messages)
        mock_mem0.add.assert_called_once_with(messages, user_id="test-agent")
