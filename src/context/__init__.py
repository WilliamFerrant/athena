"""MCP (Model Context Protocol) interception layer — living context.

Intercepts every LLM call and enriches it with dynamic context:
- Git diff / branch info from the runner
- Recent health check status
- Agent drive states
- Knowledge graph cluster context
- Recent conversation summaries from other agents

Acts as middleware between agents and the LLM backends, building a
"living context" that keeps the system aware of real-time project state.

Usage::

    from src.context.mcp_interceptor import MCPInterceptor

    mcp = MCPInterceptor(runner_client, health_store, agents)
    enriched_system = mcp.enrich(agent_id="frontend", base_system="...", task="Build nav")
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MCPInterceptor:
    """Enriches agent system prompts with live project context.

    Combines multiple context sources into a structured injection block
    that's prepended to the agent's system prompt before each LLM call.
    """

    def __init__(
        self,
        runner_client: Any | None = None,
        runner_poller: Any | None = None,
        health_store: Any | None = None,
        agents: dict[str, Any] | None = None,
        graph: Any | None = None,
    ) -> None:
        self._runner = runner_client
        self._poller = runner_poller
        self._health = health_store
        self._agents = agents or {}
        self._graph = graph

    def enrich(
        self,
        agent_id: str,
        base_system: str,
        task: str = "",
        project_id: str | None = None,
    ) -> str:
        """Return base_system enriched with living context sections."""
        sections: list[str] = []

        # 1. Runner / dev state
        runner_ctx = self._get_runner_context(project_id)
        if runner_ctx:
            sections.append(runner_ctx)

        # 2. Health status
        health_ctx = self._get_health_context(project_id)
        if health_ctx:
            sections.append(health_ctx)

        # 3. Peer agent summaries (what are others doing?)
        peer_ctx = self._get_peer_context(agent_id)
        if peer_ctx:
            sections.append(peer_ctx)

        # 4. Knowledge graph clusters
        graph_ctx = self._get_graph_context(agent_id)
        if graph_ctx:
            sections.append(graph_ctx)

        if not sections:
            return base_system

        living_ctx = (
            "\n\n--- LIVING CONTEXT (auto-injected, real-time) ---\n"
            + "\n\n".join(sections)
            + "\n--- END LIVING CONTEXT ---\n"
        )
        return base_system + living_ctx

    # -- Context sources -------------------------------------------------------

    def _get_runner_context(self, project_id: str | None) -> str:
        """Git branch, dirty files, runner status."""
        if not self._poller:
            return ""
        state = self._poller.state
        parts = [f"Runner: {'ONLINE' if state.online else 'OFFLINE'}"]
        if state.error:
            parts.append(f"  Error: {state.error}")

        if state.online and self._runner and project_id:
            try:
                git = self._runner.git_status(project_id)
                parts.append(f"  Branch: {git.branch}")
                parts.append(f"  Dirty files: {git.dirtyCount}")
                if git.changedFiles:
                    parts.append(f"  Changed: {', '.join(git.changedFiles[:10])}")
            except Exception:
                pass

        return "\n".join(parts)

    def _get_health_context(self, project_id: str | None) -> str:
        """Latest health check results for context."""
        if not self._health:
            return ""
        try:
            if project_id:
                checks = self._health.get_project_status(project_id)
            else:
                all_latest = self._health.get_all_latest()
                checks = []
                for v in all_latest.values():
                    checks.extend(v)

            if not checks:
                return ""

            lines = ["Health status:"]
            for c in checks[:5]:
                lines.append(
                    f"  {c.get('project_id', '?')}/{c.get('check_id', '?')}: "
                    f"{c.get('status', '?')} ({c.get('latency_ms', 0):.0f}ms)"
                )
            return "\n".join(lines)
        except Exception:
            return ""

    def _get_peer_context(self, current_agent_id: str) -> str:
        """Summarise what peer agents are up to."""
        if not self._agents:
            return ""
        lines = ["Team status:"]
        for aid, agent in self._agents.items():
            if aid == current_agent_id:
                continue
            drives = agent.drives.state
            conv_len = len(agent._conversation)
            status = drives.status_label()
            lines.append(
                f"  {aid}: {status} | effectiveness {drives.overall_effectiveness():.0%} "
                f"| {conv_len} msgs in conversation"
            )
        return "\n".join(lines) if len(lines) > 1 else ""

    def _get_graph_context(self, agent_id: str) -> str:
        """Knowledge graph cluster context."""
        if not self._graph:
            return ""
        try:
            return self._graph.get_context_summary(agent_id, limit=3)
        except Exception:
            return ""


# -- Singleton ------------------------------------------------------------------

_mcp: MCPInterceptor | None = None


def get_mcp() -> MCPInterceptor | None:
    """Get the global MCP interceptor (may be None if not yet initialized)."""
    return _mcp


def init_mcp(
    runner_client: Any = None,
    runner_poller: Any = None,
    health_store: Any = None,
    agents: dict[str, Any] | None = None,
    graph: Any = None,
) -> MCPInterceptor:
    """Initialize and set the global MCP interceptor."""
    global _mcp
    _mcp = MCPInterceptor(
        runner_client=runner_client,
        runner_poller=runner_poller,
        health_store=health_store,
        agents=agents,
        graph=graph,
    )
    return _mcp
