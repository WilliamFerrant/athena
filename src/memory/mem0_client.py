"""Revolving memory layer using mem0.ai + optional local knowledge graph.

Each agent gets its own memory namespace.  Memories are automatically:
- Added after meaningful interactions
- Searched before each task to provide context
- Pruned when they exceed a window (revolving)

If a ``KnowledgeGraph`` is passed at construction time, memories are also
indexed into the local graph so topological context (clusters, topic links)
can supplement vector-search results.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mem0 import MemoryClient

from src.config import settings

if TYPE_CHECKING:
    from src.memory.graph_context import KnowledgeGraph

logger = logging.getLogger(__name__)

# Maximum memories to keep per agent before pruning oldest
DEFAULT_WINDOW = 200


class AgentMemory:
    """Per-agent revolving memory backed by mem0.ai cloud.

    Optionally accepts a *graph* (``KnowledgeGraph``) to maintain a local
    topological index in addition to mem0's vector store.
    """

    def __init__(
        self,
        agent_id: str,
        api_key: str | None = None,
        window: int = DEFAULT_WINDOW,
        graph: KnowledgeGraph | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.window = window
        self._client = MemoryClient(api_key=api_key or settings.mem0_api_key)
        self._graph = graph

    # -- write -----------------------------------------------------------------

    def add(self, content: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Store a memory. Automatically prunes if over window."""
        result = self._client.add(
            content,
            user_id=self.agent_id,
            metadata=metadata or {},
        )
        self._maybe_prune()

        # Mirror into the knowledge graph if available
        if self._graph is not None:
            mem_id = _extract_id(result)
            if mem_id:
                topic = (metadata or {}).get("topic", "general")
                try:
                    self._graph.add_memory(
                        memory_id=mem_id,
                        content=content,
                        agent_id=self.agent_id,
                        topic=topic,
                        metadata=metadata,
                    )
                    # Auto-link nodes that share the same topic
                    self._graph.auto_link_by_topic(agent_id=self.agent_id)
                except Exception as exc:
                    logger.debug("Graph index failed for %s: %s", mem_id, exc)

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
        """Retrieve all memories for this agent (v2 API requires filters)."""
        try:
            results = self._client.get_all(
                version="v2",
                filters={"AND": [{"user_id": self.agent_id}]},
            )
        except Exception as e:
            logger.warning("Memory get_all failed for %s: %s", self.agent_id, e)
            return []
        if isinstance(results, dict):
            return results.get("results", results.get("memories", []))
        return results if isinstance(results, list) else []

    def get_relevant_context(self, task_description: str, limit: int = 5) -> str:
        """Return a formatted string of relevant memories for injection into prompts.

        Combines mem0 vector search results with graph-cluster context when a
        ``KnowledgeGraph`` is attached.
        """
        memories = self.search(task_description, limit=limit)
        parts: list[str] = []

        if memories:
            lines = [f"- {m.get('memory', m.get('text', str(m)))}" for m in memories]
            parts.append("Relevant memories from previous sessions:\n" + "\n".join(lines))

        # Graph context supplement
        if self._graph is not None:
            graph_ctx = self._graph.get_context_summary(self.agent_id, limit=3)
            if graph_ctx:
                parts.append(graph_ctx)

        return "\n\n".join(parts)

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
        base: dict[str, Any] = {
            "agent_id": self.agent_id,
            "memory_count": len(self.get_all()),
            "window": self.window,
        }
        if self._graph is not None:
            base["graph"] = self._graph.stats()
        return base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_id(result: Any) -> str | None:
    """Try to extract a memory ID from an add() result dict."""
    if isinstance(result, dict):
        return result.get("id") or result.get("memory_id")
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            return first.get("id") or first.get("memory_id")
    return None
