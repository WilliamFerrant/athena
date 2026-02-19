"""Revolving memory layer using mem0.ai.

Each agent gets its own memory namespace.  Memories are automatically:
- Added after meaningful interactions
- Searched before each task to provide context
- Pruned when they exceed a window (revolving)
"""

from __future__ import annotations

import logging
from typing import Any

from mem0 import MemoryClient

from src.config import settings

logger = logging.getLogger(__name__)

# Maximum memories to keep per agent before pruning oldest
DEFAULT_WINDOW = 200


class AgentMemory:
    """Per-agent revolving memory backed by mem0.ai cloud."""

    def __init__(
        self,
        agent_id: str,
        api_key: str | None = None,
        window: int = DEFAULT_WINDOW,
    ) -> None:
        self.agent_id = agent_id
        self.window = window
        self._client = MemoryClient(api_key=api_key or settings.mem0_api_key)

    # -- write -----------------------------------------------------------------

    def add(self, content: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Store a memory. Automatically prunes if over window."""
        result = self._client.add(
            content,
            user_id=self.agent_id,
            metadata=metadata or {},
        )
        self._maybe_prune()
        return result

    def add_conversation(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """Extract and store memories from a conversation."""
        return self._client.add(
            messages,
            user_id=self.agent_id,
        )

    # -- read ------------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Semantic search over this agent's memories (v2 API with filters)."""
        try:
            results = self._client.search(
                query,
                version="v2",
                filters={"OR": [{"user_id": self.agent_id}]},
                limit=limit,
            )
        except Exception as e:
            logger.warning("Memory search failed for %s: %s", self.agent_id, e)
            return []
        if isinstance(results, dict):
            return results.get("results", results.get("memories", []))
        return results if isinstance(results, list) else []

    def get_all(self) -> list[dict[str, Any]]:
        """Retrieve all memories for this agent."""
        try:
            results = self._client.get_all(user_id=self.agent_id)
        except Exception as e:
            logger.warning("Memory get_all failed for %s: %s", self.agent_id, e)
            return []
        if isinstance(results, dict):
            return results.get("results", results.get("memories", []))
        return results if isinstance(results, list) else []

    def get_relevant_context(self, task_description: str, limit: int = 5) -> str:
        """Return a formatted string of relevant memories for injection into prompts."""
        memories = self.search(task_description, limit=limit)
        if not memories:
            return ""
        lines = [f"- {m.get('memory', m.get('text', str(m)))}" for m in memories]
        return "Relevant memories from previous sessions:\n" + "\n".join(lines)

    # -- maintenance -----------------------------------------------------------

    def _maybe_prune(self) -> None:
        """If memories exceed window, delete the oldest ones."""
        all_mems = self.get_all()
        if len(all_mems) <= self.window:
            return
        sorted_mems = sorted(all_mems, key=lambda m: m.get("created_at", ""))
        to_delete = sorted_mems[: len(sorted_mems) - self.window]
        for mem in to_delete:
            mem_id = mem.get("id")
            if mem_id:
                try:
                    self._client.delete(mem_id)
                except Exception:
                    logger.warning("Failed to prune memory %s", mem_id)

    def clear(self) -> None:
        """Delete all memories for this agent."""
        try:
            self._client.delete_all(user_id=self.agent_id)
        except Exception as e:
            logger.warning("Memory clear failed for %s: %s", self.agent_id, e)

    def stats(self) -> dict[str, Any]:
        all_mems = self.get_all()
        return {
            "agent_id": self.agent_id,
            "memory_count": len(all_mems),
            "window": self.window,
        }
