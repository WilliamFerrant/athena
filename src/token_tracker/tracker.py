"""Token usage tracker wrapping Claude Code CLI + real session data.

Two responsibilities:
1. **CLI calls**: Agents call `create_message()` which invokes `claude -p` via
   subprocess.  Tracks per-agent call counts / char counts for this process.
2. **Real usage data**: `get_real_usage()` parses ~/.claude session JSONL files
   for actual token counts from your Claude Code subscription — the same data
   that claude-spend reads.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from src.config import settings
from src.token_tracker.session_parser import (
    UsageReport,
    compute_rate_limits,
    parse_all_sessions,
    report_to_dict,
)

logger = logging.getLogger(__name__)


@dataclass
class UsageRecord:
    """Single CLI call usage snapshot."""

    agent_id: str
    model: str
    input_chars: int
    output_chars: int
    cost_usd: float = 0.0  # Always 0 — uses subscription
    timestamp: float = field(default_factory=time.time)
    latency_ms: float = 0.0


class ClaudeResponse:
    """Mimics the shape of an Anthropic API response for compatibility."""

    def __init__(self, text: str, input_chars: int) -> None:
        self.text = text
        self.content = [_TextBlock(text)]
        self.input_chars = input_chars
        self.output_chars = len(text)


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class TokenTracker:
    """Tracks Claude Code CLI calls + reads real session data from ~/.claude."""

    def __init__(self, api_key: str | None = None) -> None:
        # api_key kept for interface compatibility but unused
        self.records: list[UsageRecord] = []
        self._call_count: int = 0
        self._daily_limit: int = settings.daily_call_limit
        self._claude_cmd: str = settings.claude_cli_path
        # Cached real usage data (parsed from ~/.claude)
        self._cached_report: UsageReport | None = None

    # -- Real usage data (from ~/.claude JSONL files) --------------------------

    def get_real_usage(self, force_refresh: bool = False) -> UsageReport:
        """Parse and return actual token usage from Claude Code session files.

        The first call parses all session files; subsequent calls return
        cached data unless force_refresh=True.
        """
        if self._cached_report is None or force_refresh:
            self._cached_report = parse_all_sessions()
        return self._cached_report

    def get_real_usage_dict(self, force_refresh: bool = False) -> dict[str, Any]:
        """Like get_real_usage() but returns a JSON-serializable dict."""
        report = self.get_real_usage(force_refresh=force_refresh)
        return report_to_dict(report)

    def refresh_real_usage(self) -> dict[str, Any]:
        """Force re-parse session files and return updated data."""
        return self.get_real_usage_dict(force_refresh=True)

    def get_rate_limits(self) -> dict[str, Any]:
        """Compute tokens used within rolling session/weekly windows."""
        return compute_rate_limits(
            session_cap=settings.session_limit_tokens,
            weekly_cap=settings.weekly_limit_tokens,
            session_window_hours=settings.session_window_hours,
            weekly_window_days=settings.weekly_window_days,
        )

    # -- budget helpers --------------------------------------------------------

    @property
    def total_cost(self) -> float:
        return 0.0  # Subscription-based, no per-call cost

    @property
    def budget_remaining(self) -> int:
        return max(0, self._daily_limit - self._call_count)

    @property
    def is_over_budget(self) -> bool:
        return self._call_count >= self._daily_limit

    # -- per-agent summaries ---------------------------------------------------

    def agent_summary(self, agent_id: str) -> dict[str, Any]:
        agent_records = [r for r in self.records if r.agent_id == agent_id]
        return {
            "agent_id": agent_id,
            "calls": len(agent_records),
            "input_chars": sum(r.input_chars for r in agent_records),
            "output_chars": sum(r.output_chars for r in agent_records),
            "total_cost_usd": 0.0,
        }

    def all_agents_summary(self) -> list[dict[str, Any]]:
        agents = {r.agent_id for r in self.records}
        return [self.agent_summary(a) for a in sorted(agents)]

    def global_summary(self) -> dict[str, Any]:
        """Summary of this process session's CLI calls + real usage totals."""
        real = self.get_real_usage()
        return {
            "total_calls": len(self.records),
            "total_input_chars": sum(r.input_chars for r in self.records),
            "total_output_chars": sum(r.output_chars for r in self.records),
            "total_cost_usd": 0.0,
            "daily_call_limit": self._daily_limit,
            "calls_remaining": self.budget_remaining,
            "agents": self.all_agents_summary(),
            # Real tracked data from ~/.claude
            "real_usage": real.totals,
        }

    # -- core: call Claude Code CLI --------------------------------------------

    def create_message(
        self,
        *,
        agent_id: str,
        model: str | None = None,
        max_tokens: int = 4096,
        system: str | None = None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ClaudeResponse:
        """Call Claude Code CLI in print mode and track usage."""
        if self.is_over_budget:
            raise RuntimeError(
                f"Daily call limit of {self._daily_limit} reached "
                f"({self._call_count} calls made)"
            )

        # Build the prompt from system + messages
        prompt = self._build_prompt(system, messages)

        # Build CLI command
        cmd = [self._claude_cmd, "-p"]
        if model:
            cmd.extend(["--model", model])
        if max_tokens:
            cmd.extend(["--max-tokens", str(max_tokens)])

        t0 = time.perf_counter()
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300,  # 5 min timeout
            )
            output = result.stdout.strip()
            if result.returncode != 0 and not output:
                output = f"Error: {result.stderr.strip() or 'Claude CLI returned non-zero'}"
        except subprocess.TimeoutExpired:
            output = "Error: Claude CLI timed out after 300s"
        except FileNotFoundError:
            output = (
                f"Error: Claude CLI not found at '{self._claude_cmd}'. "
                "Make sure Claude Code is installed: npm install -g @anthropic-ai/claude-code"
            )

        latency = (time.perf_counter() - t0) * 1000

        record = UsageRecord(
            agent_id=agent_id,
            model=model or settings.default_model,
            input_chars=len(prompt),
            output_chars=len(output),
            latency_ms=latency,
        )
        self.records.append(record)
        self._call_count += 1

        return ClaudeResponse(text=output, input_chars=len(prompt))

    async def acreate_message(
        self,
        *,
        agent_id: str,
        model: str | None = None,
        max_tokens: int = 4096,
        system: str | None = None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ClaudeResponse:
        """Async version — runs CLI in a thread to avoid blocking."""
        import asyncio

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.create_message(
                agent_id=agent_id,
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=tools,
                **kwargs,
            ),
        )

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _build_prompt(
        system: str | None,
        messages: list[dict[str, Any]],
    ) -> str:
        """Convert system prompt + message history into a single prompt string."""
        parts: list[str] = []

        if system:
            parts.append(f"[System]\n{system}")

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                # Handle structured content blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif hasattr(block, "text"):
                        text_parts.append(block.text)
                content = "\n".join(text_parts)
            parts.append(f"[{role.title()}]\n{content}")

        return "\n\n".join(parts)

    def reset_daily(self) -> None:
        """Reset the daily call counter."""
        self._call_count = 0
