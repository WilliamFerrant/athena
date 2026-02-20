"""Local in-memory knowledge graph for living context.

Uses NetworkX to maintain a directed graph of memories linked by topic,
agent, and semantic proximity.  Acts as a topological layer on top of the
vector-search provided by mem0.ai — retrieving *clusters* of related nodes
rather than just individual similarity hits.

The graph is a **process-level singleton** (not persisted across restarts).
mem0 remains the durable storage; the graph enriches in-session retrieval.

Usage::

    from src.memory.graph_context import get_shared_graph

    graph = get_shared_graph()
    graph.add_memory("mem-001", "User prefers React hooks", agent_id="frontend", topic="react")
    graph.add_memory("mem-002", "Avoid class components", agent_id="frontend", topic="react")
    graph.link("mem-001", "mem-002", relation="constrains")

    cluster = graph.get_cluster("mem-001")
    print(graph.stats())
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    import networkx as nx

    _NX_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NX_AVAILABLE = False
    nx = None  # type: ignore[assignment]


class KnowledgeGraph:
    """Directed graph of agent memories linked by topic / project / relation."""

    def __init__(self) -> None:
        if not _NX_AVAILABLE:
            raise ImportError(
                "networkx is required for KnowledgeGraph. "
                "Install it: pip install networkx"
            )
        self._graph: Any = nx.DiGraph()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add_memory(
        self,
        memory_id: str,
        content: str,
        agent_id: str,
        topic: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add (or update) a memory node to the graph."""
        attrs: dict[str, Any] = {
            "content": content,
            "agent": agent_id,
            "topic": topic or "general",
        }
        if metadata:
            attrs.update(metadata)
        self._graph.add_node(memory_id, **attrs)

    def link(self, from_id: str, to_id: str, relation: str = "related") -> None:
        """Create a directed edge between two existing memory nodes."""
        if from_id in self._graph and to_id in self._graph:
            self._graph.add_edge(from_id, to_id, relation=relation)
        else:
            logger.debug("link(%s → %s) skipped — one or both nodes missing", from_id, to_id)

    def auto_link_by_topic(self, agent_id: str | None = None) -> int:
        """Auto-connect all nodes that share the same topic (and optionally agent).

        Returns the number of edges added.
        """
        nodes = list(self._graph.nodes(data=True))
        added = 0
        for i, (nid_a, attrs_a) in enumerate(nodes):
            for nid_b, attrs_b in nodes[i + 1 :]:
                same_topic = attrs_a.get("topic") == attrs_b.get("topic")
                same_agent = agent_id is None or (
                    attrs_a.get("agent") == agent_id and attrs_b.get("agent") == agent_id
                )
                if same_topic and same_agent and not self._graph.has_edge(nid_a, nid_b):
                    self._graph.add_edge(nid_a, nid_b, relation="same_topic")
                    added += 1
        return added

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_cluster(self, memory_id: str, depth: int = 2) -> list[dict[str, Any]]:
        """Return all nodes within *depth* hops of *memory_id* (ego graph)."""
        if memory_id not in self._graph:
            return []
        ego = nx.ego_graph(self._graph, memory_id, radius=depth, undirected=True)  # type: ignore[union-attr]
        return [{"id": nid, **attrs} for nid, attrs in ego.nodes(data=True)]

    def search_by_topic(self, topic: str, agent_id: str | None = None) -> list[dict[str, Any]]:
        """Return all nodes whose topic matches (case-insensitive)."""
        results = []
        for nid, attrs in self._graph.nodes(data=True):
            if attrs.get("topic", "").lower() != topic.lower():
                continue
            if agent_id and attrs.get("agent") != agent_id:
                continue
            results.append({"id": nid, **attrs})
        return results

    def search_by_agent(self, agent_id: str) -> list[dict[str, Any]]:
        """Return all nodes belonging to *agent_id*."""
        return [
            {"id": nid, **attrs}
            for nid, attrs in self._graph.nodes(data=True)
            if attrs.get("agent") == agent_id
        ]

    def get_context_summary(self, agent_id: str, limit: int = 5) -> str:
        """Return a formatted string of the agent's most connected nodes."""
        agent_nodes = [
            (nid, self._graph.degree(nid))
            for nid, attrs in self._graph.nodes(data=True)
            if attrs.get("agent") == agent_id
        ]
        # Sort by degree descending (most connected = most relevant)
        agent_nodes.sort(key=lambda x: x[1], reverse=True)
        top = agent_nodes[:limit]
        if not top:
            return ""
        lines = ["Graph context (most connected memories):"]
        for nid, degree in top:
            content = self._graph.nodes[nid].get("content", "")
            topic = self._graph.nodes[nid].get("topic", "")
            lines.append(f"  [{topic}] {content[:120]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Serialisation / stats
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return the full graph as a JSON-serialisable dict."""
        return {
            "nodes": [{"id": n, **d} for n, d in self._graph.nodes(data=True)],
            "edges": [
                {"from": u, "to": v, **d} for u, v, d in self._graph.edges(data=True)
            ],
        }

    def stats(self) -> dict[str, int]:
        return {
            "nodes": self._graph.number_of_nodes(),
            "edges": self._graph.number_of_edges(),
        }

    def clear(self) -> None:
        self._graph.clear()


# ---------------------------------------------------------------------------
# Process-level singleton
# ---------------------------------------------------------------------------

_shared_graph: KnowledgeGraph | None = None


def get_shared_graph() -> KnowledgeGraph:
    """Return the process-level singleton KnowledgeGraph (created lazily)."""
    global _shared_graph
    if _shared_graph is None:
        try:
            _shared_graph = KnowledgeGraph()
        except ImportError:
            logger.warning(
                "networkx not installed — KnowledgeGraph unavailable. "
                "Install with: pip install networkx"
            )
            raise
    return _shared_graph
