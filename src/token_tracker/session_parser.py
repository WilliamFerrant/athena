"""Parse real Claude Code session data from ~/.claude directory.

A Python port of claude-spend (https://github.com/writetoaniketparihar-collab/claude-spend).
Reads JSONL session files that Claude Code creates locally to provide
actual token usage data — no estimation or guessing.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _claude_dir() -> Path:
    """Return the path to the ~/.claude directory."""
    return Path.home() / ".claude"


@dataclass
class QueryRecord:
    """A single assistant response with token usage."""

    user_prompt: str | None
    user_timestamp: str | None
    assistant_timestamp: str | None
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass
class SessionData:
    """Parsed data for a single Claude Code session."""

    session_id: str
    project: str
    date: str
    timestamp: str | None
    first_prompt: str
    model: str
    query_count: int
    queries: list[QueryRecord]
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass
class DailyUsage:
    """Aggregated usage for a single day."""

    date: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    sessions: int = 0
    queries: int = 0


@dataclass
class ModelBreakdown:
    """Token usage broken down by model."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    query_count: int = 0


@dataclass
class TopPrompt:
    """A prompt ranked by token cost."""

    prompt: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    date: str
    session_id: str
    model: str


@dataclass
class Insight:
    """An observation about usage patterns."""

    id: str
    type: str  # "warning", "info", "neutral"
    title: str
    description: str
    action: str | None = None


@dataclass
class UsageReport:
    """Complete parsed usage report."""

    sessions: list[SessionData] = field(default_factory=list)
    daily_usage: list[DailyUsage] = field(default_factory=list)
    model_breakdown: list[ModelBreakdown] = field(default_factory=list)
    top_prompts: list[TopPrompt] = field(default_factory=list)
    totals: dict[str, Any] = field(default_factory=dict)
    insights: list[Insight] = field(default_factory=list)


def _fmt(n: int) -> str:
    """Format a number for display."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n // 1_000}K"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


def _parse_jsonl_file(file_path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file, skipping malformed lines."""
    entries: list[dict[str, Any]] = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError as e:
        logger.debug("Could not read %s: %s", file_path, e)
    return entries


def _extract_session_data(entries: list[dict[str, Any]]) -> list[QueryRecord]:
    """Extract query records from JSONL entries."""
    queries: list[QueryRecord] = []
    pending_user_message: dict[str, Any] | None = None

    for entry in entries:
        # Track user messages
        if entry.get("type") == "user" and entry.get("message", {}).get("role") == "user":
            content = entry.get("message", {}).get("content", "")
            if entry.get("isMeta"):
                continue
            if isinstance(content, str) and (
                content.startswith("<local-command")
                or content.startswith("<command-")
            ):
                continue

            text = content if isinstance(content, str) else json.dumps(content)
            pending_user_message = {
                "text": text,
                "timestamp": entry.get("timestamp"),
            }

        # Track assistant responses with usage data
        if entry.get("type") == "assistant" and entry.get("message", {}).get("usage"):
            usage = entry["message"]["usage"]
            model = entry["message"].get("model", "unknown")
            if model == "<synthetic>":
                continue

            input_tokens = (
                (usage.get("input_tokens", 0) or 0)
                + (usage.get("cache_creation_input_tokens", 0) or 0)
                + (usage.get("cache_read_input_tokens", 0) or 0)
            )
            output_tokens = usage.get("output_tokens", 0) or 0

            queries.append(
                QueryRecord(
                    user_prompt=pending_user_message["text"] if pending_user_message else None,
                    user_timestamp=pending_user_message["timestamp"] if pending_user_message else None,
                    assistant_timestamp=entry.get("timestamp"),
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                )
            )

    return queries


def parse_all_sessions(claude_dir: Path | None = None) -> UsageReport:
    """Parse all Claude Code session data and return a complete usage report.

    Args:
        claude_dir: Override path to .claude directory (useful for testing).
                    Defaults to ~/.claude.
    """
    claude_dir = claude_dir or _claude_dir()
    projects_dir = claude_dir / "projects"

    if not projects_dir.exists():
        return UsageReport()

    # Read history.jsonl for session display text
    history_path = claude_dir / "history.jsonl"
    history_entries = _parse_jsonl_file(history_path) if history_path.exists() else []

    # Build session_id -> first meaningful prompt map
    session_first_prompt: dict[str, str] = {}
    for entry in history_entries:
        sid = entry.get("sessionId")
        display = entry.get("display", "").strip()
        if sid and display and sid not in session_first_prompt:
            if display.startswith("/") and len(display) < 30:
                continue
            session_first_prompt[sid] = display

    sessions: list[SessionData] = []
    daily_map: dict[str, DailyUsage] = {}
    model_map: dict[str, ModelBreakdown] = {}
    all_prompts: list[TopPrompt] = []

    # Iterate all project directories
    try:
        project_dirs = [
            d for d in projects_dir.iterdir()
            if d.is_dir()
        ]
    except OSError:
        return UsageReport()

    for proj_dir in project_dirs:
        jsonl_files = list(proj_dir.glob("*.jsonl"))

        for jsonl_file in jsonl_files:
            session_id = jsonl_file.stem
            entries = _parse_jsonl_file(jsonl_file)
            if not entries:
                continue

            queries = _extract_session_data(entries)
            if not queries:
                continue

            input_tokens = sum(q.input_tokens for q in queries)
            output_tokens = sum(q.output_tokens for q in queries)
            total_tokens = input_tokens + output_tokens

            # Find first timestamp
            first_timestamp = next(
                (e.get("timestamp") for e in entries if e.get("timestamp")),
                None,
            )
            if first_timestamp and isinstance(first_timestamp, str):
                date = first_timestamp.split("T")[0]
            else:
                date = "unknown"

            # Primary model (most used)
            model_counts: dict[str, int] = {}
            for q in queries:
                model_counts[q.model] = model_counts.get(q.model, 0) + 1
            primary_model = (
                max(model_counts, key=model_counts.get) if model_counts else "unknown"  # type: ignore[arg-type]
            )

            # First prompt
            first_prompt = (
                session_first_prompt.get(session_id)
                or next((q.user_prompt for q in queries if q.user_prompt), None)
                or "(no prompt)"
            )

            # Collect per-prompt data for "most expensive prompts"
            current_prompt: str | None = None
            prompt_input = 0
            prompt_output = 0

            def flush_prompt() -> None:
                nonlocal current_prompt, prompt_input, prompt_output
                if current_prompt and (prompt_input + prompt_output) > 0:
                    all_prompts.append(
                        TopPrompt(
                            prompt=current_prompt[:300],
                            input_tokens=prompt_input,
                            output_tokens=prompt_output,
                            total_tokens=prompt_input + prompt_output,
                            date=date,
                            session_id=session_id,
                            model=primary_model,
                        )
                    )

            for q in queries:
                if q.user_prompt and q.user_prompt != current_prompt:
                    flush_prompt()
                    current_prompt = q.user_prompt
                    prompt_input = 0
                    prompt_output = 0
                prompt_input += q.input_tokens
                prompt_output += q.output_tokens
            flush_prompt()

            sessions.append(
                SessionData(
                    session_id=session_id,
                    project=proj_dir.name,
                    date=date,
                    timestamp=first_timestamp if isinstance(first_timestamp, str) else None,
                    first_prompt=first_prompt[:200],
                    model=primary_model,
                    query_count=len(queries),
                    queries=queries,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                )
            )

            # Daily aggregation
            if date != "unknown":
                if date not in daily_map:
                    daily_map[date] = DailyUsage(date=date)
                day = daily_map[date]
                day.input_tokens += input_tokens
                day.output_tokens += output_tokens
                day.total_tokens += total_tokens
                day.sessions += 1
                day.queries += len(queries)

            # Model aggregation
            for q in queries:
                if q.model in ("<synthetic>", "unknown"):
                    continue
                if q.model not in model_map:
                    model_map[q.model] = ModelBreakdown(model=q.model)
                mb = model_map[q.model]
                mb.input_tokens += q.input_tokens
                mb.output_tokens += q.output_tokens
                mb.total_tokens += q.total_tokens
                mb.query_count += 1

    # Sort sessions by total tokens descending
    sessions.sort(key=lambda s: s.total_tokens, reverse=True)

    # Sort daily usage chronologically
    daily_usage = sorted(daily_map.values(), key=lambda d: d.date)

    # Top 20 most expensive prompts
    all_prompts.sort(key=lambda p: p.total_tokens, reverse=True)
    top_prompts = all_prompts[:20]

    # Grand totals
    total_sessions = len(sessions)
    total_queries = sum(s.query_count for s in sessions)
    total_tokens = sum(s.total_tokens for s in sessions)
    total_input = sum(s.input_tokens for s in sessions)
    total_output = sum(s.output_tokens for s in sessions)

    totals = {
        "total_sessions": total_sessions,
        "total_queries": total_queries,
        "total_tokens": total_tokens,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "avg_tokens_per_query": round(total_tokens / total_queries) if total_queries else 0,
        "avg_tokens_per_session": round(total_tokens / total_sessions) if total_sessions else 0,
        "date_range": (
            {"from": daily_usage[0].date, "to": daily_usage[-1].date}
            if daily_usage
            else None
        ),
    }

    # Generate insights
    insights = _generate_insights(sessions, all_prompts, totals)

    return UsageReport(
        sessions=sessions,
        daily_usage=daily_usage,
        model_breakdown=list(model_map.values()),
        top_prompts=top_prompts,
        totals=totals,
        insights=insights,
    )


def _generate_insights(
    sessions: list[SessionData],
    all_prompts: list[TopPrompt],
    totals: dict[str, Any],
) -> list[Insight]:
    """Generate actionable insights from usage data."""
    insights: list[Insight] = []

    # 1. Short, vague messages that cost a lot
    short_expensive = [
        p for p in all_prompts
        if len(p.prompt.strip()) < 30 and p.total_tokens > 100_000
    ]
    if short_expensive:
        total_wasted = sum(p.total_tokens for p in short_expensive)
        examples = list(dict.fromkeys(p.prompt.strip() for p in short_expensive))[:4]
        examples_str = ", ".join(f'"{e}"' for e in examples)
        insights.append(
            Insight(
                id="vague-prompts",
                type="warning",
                title="Short, vague messages are costing you the most",
                description=(
                    f"{len(short_expensive)} times you sent a short message like {examples_str} "
                    f"— and each time, Claude used over 100K tokens to respond. "
                    f"That adds up to {_fmt(total_wasted)} tokens total."
                ),
                action=(
                    'Try being specific. Instead of "Yes", say '
                    '"Yes, update the login page and run the tests."'
                ),
            )
        )

    # 2. Long conversations getting more expensive over time
    long_sessions = [s for s in sessions if len(s.queries) > 50]
    if long_sessions:
        growth_data = []
        for s in long_sessions:
            first5 = sum(q.total_tokens for q in s.queries[:5]) / min(5, len(s.queries))
            last5 = sum(q.total_tokens for q in s.queries[-5:]) / min(5, len(s.queries))
            ratio = last5 / max(first5, 1)
            if ratio > 2:
                growth_data.append({"session": s, "ratio": ratio})

        if growth_data:
            avg_growth = sum(g["ratio"] for g in growth_data) / len(growth_data)
            worst = max(growth_data, key=lambda g: g["ratio"])
            insights.append(
                Insight(
                    id="context-growth",
                    type="warning",
                    title="The longer you chat, the more each message costs",
                    description=(
                        f"In {len(growth_data)} conversations, messages near the end cost "
                        f"{avg_growth:.1f}x more than at the start. "
                        f'Your worst ("{ worst["session"].first_prompt[:50]}...") grew '
                        f'{worst["ratio"]:.1f}x more expensive by the end.'
                    ),
                    action=(
                        "Start a fresh conversation when you move to a new task. "
                        "Paste a short summary in your first message for context."
                    ),
                )
            )

    # 3. Marathon conversations
    turn_counts = sorted(s.query_count for s in sessions)
    median_turns = turn_counts[len(turn_counts) // 2] if turn_counts else 0
    long_count = sum(1 for s in sessions if s.query_count > 200)
    if long_count >= 3 and totals.get("total_tokens", 0) > 0:
        long_tokens = sum(s.total_tokens for s in sessions if s.query_count > 200)
        long_pct = round(long_tokens / totals["total_tokens"] * 100)
        insights.append(
            Insight(
                id="marathon-sessions",
                type="info",
                title=f"Just {long_count} long conversations used {long_pct}% of all your tokens",
                description=(
                    f"You have {long_count} conversations with 200+ messages, consuming "
                    f"{_fmt(long_tokens)} tokens ({long_pct}%). "
                    f"Typical conversation: ~{median_turns} messages."
                ),
                action="Keep one conversation per task. Start a new one when the topic drifts.",
            )
        )

    # 4. Most tokens are re-reading, not writing
    if totals.get("total_tokens", 0) > 0:
        output_pct = totals.get("total_output_tokens", 0) / totals["total_tokens"] * 100
        if output_pct < 2:
            insights.append(
                Insight(
                    id="input-heavy",
                    type="info",
                    title=f"{output_pct:.1f}% of your tokens are Claude actually writing",
                    description=(
                        f"Out of {_fmt(totals['total_tokens'])} total tokens, only "
                        f"{_fmt(totals['total_output_tokens'])} are Claude writing responses. "
                        f"The other {100 - output_pct:.1f}% is re-reading conversation history."
                    ),
                    action=(
                        "Keeping conversations shorter has more impact than asking for shorter answers."
                    ),
                )
            )

    # 5. Day-of-week pattern
    if len(sessions) >= 10:
        day_map: dict[int, dict[str, int]] = {}
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        for s in sessions:
            if not s.timestamp:
                continue
            try:
                dt = datetime.fromisoformat(s.timestamp.replace("Z", "+00:00"))
                day = dt.weekday()
            except (ValueError, AttributeError):
                continue
            if day not in day_map:
                day_map[day] = {"tokens": 0, "sessions": 0}
            day_map[day]["tokens"] += s.total_tokens
            day_map[day]["sessions"] += 1

        days = [
            {"day": day_names[d], "avg": v["tokens"] / max(v["sessions"], 1)}
            for d, v in day_map.items()
        ]
        if len(days) >= 3:
            days.sort(key=lambda x: x["avg"], reverse=True)
            busiest = days[0]
            quietest = days[-1]
            insights.append(
                Insight(
                    id="day-pattern",
                    type="neutral",
                    title=f'You use Claude the most on {busiest["day"]}s',
                    description=(
                        f'{busiest["day"]} sessions average {_fmt(round(busiest["avg"]))} tokens, '
                        f'vs {_fmt(round(quietest["avg"]))} on {quietest["day"]}s.'
                    ),
                )
            )

    # 6. Model mismatch — Opus for simple tasks
    opus_sessions = [s for s in sessions if "opus" in s.model.lower()]
    if opus_sessions:
        simple_opus = [s for s in opus_sessions if s.query_count < 10 and s.total_tokens < 200_000]
        if len(simple_opus) >= 3:
            wasted = sum(s.total_tokens for s in simple_opus)
            examples = ", ".join(
                f'"{s.first_prompt[:40]}"' for s in simple_opus[:3]
            )
            insights.append(
                Insight(
                    id="model-mismatch",
                    type="warning",
                    title=f"{len(simple_opus)} simple conversations used Opus unnecessarily",
                    description=(
                        f"Short conversations ({_fmt(wasted)} tokens on Opus): {examples}. "
                        "Sonnet or Haiku would give similar results at lower cost."
                    ),
                    action="Use /model to switch to Sonnet for simple tasks.",
                )
            )

    # 7. One project dominates
    if len(sessions) >= 5 and totals.get("total_tokens", 0) > 0:
        project_tokens: dict[str, int] = {}
        for s in sessions:
            proj = s.project or "unknown"
            project_tokens[proj] = project_tokens.get(proj, 0) + s.total_tokens
        sorted_projects = sorted(project_tokens.items(), key=lambda x: x[1], reverse=True)
        if len(sorted_projects) >= 2:
            top_project, top_tokens = sorted_projects[0]
            pct = round(top_tokens / totals["total_tokens"] * 100)
            if pct >= 60:
                # Clean up project directory name for display
                name = top_project.replace("C--Users-", "~").replace("-", "/")
                insights.append(
                    Insight(
                        id="project-dominance",
                        type="info",
                        title=f"{pct}% of your tokens went to one project: {name}",
                        description=(
                            f'"{name}" used {_fmt(top_tokens)} of {_fmt(totals["total_tokens"])} '
                            f"total tokens ({pct}%)."
                        ),
                        action="If this project has long conversations, breaking them up could help.",
                    )
                )

    return insights


# -- Serialization helpers -----------------------------------------------------


def _query_to_dict(q: QueryRecord) -> dict[str, Any]:
    return {
        "user_prompt": q.user_prompt,
        "user_timestamp": q.user_timestamp,
        "assistant_timestamp": q.assistant_timestamp,
        "model": q.model,
        "input_tokens": q.input_tokens,
        "output_tokens": q.output_tokens,
        "total_tokens": q.total_tokens,
    }


def _session_to_dict(s: SessionData) -> dict[str, Any]:
    return {
        "session_id": s.session_id,
        "project": s.project,
        "date": s.date,
        "timestamp": s.timestamp,
        "first_prompt": s.first_prompt,
        "model": s.model,
        "query_count": s.query_count,
        "input_tokens": s.input_tokens,
        "output_tokens": s.output_tokens,
        "total_tokens": s.total_tokens,
    }


def report_to_dict(report: UsageReport) -> dict[str, Any]:
    """Convert a UsageReport to a JSON-serializable dict."""
    return {
        "sessions": [_session_to_dict(s) for s in report.sessions],
        "daily_usage": [
            {
                "date": d.date,
                "input_tokens": d.input_tokens,
                "output_tokens": d.output_tokens,
                "total_tokens": d.total_tokens,
                "sessions": d.sessions,
                "queries": d.queries,
            }
            for d in report.daily_usage
        ],
        "model_breakdown": [
            {
                "model": m.model,
                "input_tokens": m.input_tokens,
                "output_tokens": m.output_tokens,
                "total_tokens": m.total_tokens,
                "query_count": m.query_count,
            }
            for m in report.model_breakdown
        ],
        "top_prompts": [
            {
                "prompt": p.prompt,
                "input_tokens": p.input_tokens,
                "output_tokens": p.output_tokens,
                "total_tokens": p.total_tokens,
                "date": p.date,
                "session_id": p.session_id,
                "model": p.model,
            }
            for p in report.top_prompts
        ],
        "totals": report.totals,
        "insights": [
            {
                "id": i.id,
                "type": i.type,
                "title": i.title,
                "description": i.description,
                "action": i.action,
            }
            for i in report.insights
        ],
    }


# ── Rate-limit estimation ────────────────────────────────────────────────────


@dataclass
class RateLimitWindow:
    """Token usage in a rolling time window with estimated limit."""

    name: str  # "session" or "weekly"
    window_label: str  # e.g. "5hr" or "7 day"
    tokens_used: int
    token_cap: int
    percent_used: float  # 0..100, capped at 100
    resets_in_seconds: int  # seconds until oldest token in window expires
    resets_in_label: str  # human readable "2h 13m" / "6d 4h"
    query_count: int
    window_start_iso: str  # ISO timestamp of window start
    window_end_iso: str  # ISO timestamp of window end (now)


def compute_rate_limits(
    claude_dir: Path | None = None,
    *,
    session_cap: int = 14_000_000,
    weekly_cap: int = 120_000_000,
    session_window_hours: int = 5,
    weekly_window_days: int = 7,
) -> dict[str, Any]:
    """Compute token usage in rolling session (5hr) and weekly (7d) windows.

    Scans all JSONL files for assistant responses with timestamps within
    the time windows and sums their token counts.

    Args:
        claude_dir: Override path to .claude directory.
        session_cap: Estimated session token cap.
        weekly_cap: Estimated weekly token cap.
        session_window_hours: Rolling session window in hours.
        weekly_window_days: Rolling weekly window in days.

    Returns:
        Dict with 'session' and 'weekly' RateLimitWindow dicts, plus metadata.
    """
    from datetime import timedelta, timezone

    claude_dir = claude_dir or _claude_dir()
    projects_dir = claude_dir / "projects"
    now = datetime.now(timezone.utc)

    session_cutoff = now - timedelta(hours=session_window_hours)
    weekly_cutoff = now - timedelta(days=weekly_window_days)

    session_tokens = 0
    session_queries = 0
    session_oldest_ts: datetime | None = None

    weekly_tokens = 0
    weekly_queries = 0
    weekly_oldest_ts: datetime | None = None

    if projects_dir.exists():
        try:
            jsonl_files = list(projects_dir.rglob("*.jsonl"))
        except OSError:
            jsonl_files = []

        for jsonl_file in jsonl_files:
            # Quick stat check — skip files not modified in the weekly window
            try:
                mtime = jsonl_file.stat().st_mtime
                file_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
                if file_dt < weekly_cutoff:
                    continue
            except OSError:
                continue

            for line in _iter_file_lines(jsonl_file):
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if entry.get("type") != "assistant":
                    continue

                usage = entry.get("message", {}).get("usage")
                if not usage:
                    continue

                model = entry.get("message", {}).get("model", "")
                if model == "<synthetic>":
                    continue

                ts_str = entry.get("timestamp")
                if not ts_str:
                    continue

                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue

                tokens = (
                    (usage.get("input_tokens", 0) or 0)
                    + (usage.get("cache_creation_input_tokens", 0) or 0)
                    + (usage.get("cache_read_input_tokens", 0) or 0)
                    + (usage.get("output_tokens", 0) or 0)
                )

                # Weekly window
                if ts >= weekly_cutoff:
                    weekly_tokens += tokens
                    weekly_queries += 1
                    if weekly_oldest_ts is None or ts < weekly_oldest_ts:
                        weekly_oldest_ts = ts

                # Session window
                if ts >= session_cutoff:
                    session_tokens += tokens
                    session_queries += 1
                    if session_oldest_ts is None or ts < session_oldest_ts:
                        session_oldest_ts = ts

    # Compute reset times
    def _reset_seconds(oldest_ts: datetime | None, cutoff: datetime) -> int:
        """Seconds until the oldest entry in the window expires."""
        if oldest_ts is None:
            return 0
        # The oldest entry exits the window when: oldest_ts + window_duration = reset_time
        # But we compute from now: reset_in = (oldest_ts - cutoff_start) mapped to future
        # Actually: the window slides forward. The oldest entry exits when
        # now + (oldest_ts - cutoff) seconds pass... Simpler:
        # reset_at = oldest_ts + window_duration (from its perspective)
        # but since the window is relative to now, once now > oldest_ts + window,
        # that entry is already gone.  For rolling window:
        # reset_at is when the window no longer includes the bulk of usage,
        # i.e., how long until the window has slid past the heavy usage.
        # Simplest meaningful answer: time until window slides past oldest entry
        # = (oldest_ts + window_size) - now
        return 0  # placeholder, will compute below

    def _compute_window(
        name: str,
        label: str,
        tokens: int,
        queries: int,
        cap: int,
        cutoff: datetime,
        oldest_ts: datetime | None,
        window_delta: timedelta,
    ) -> dict[str, Any]:
        pct = min(100.0, (tokens / cap * 100)) if cap > 0 else 0.0

        # Reset time: when does the oldest activity in window expire?
        if oldest_ts is not None:
            reset_at = oldest_ts + window_delta
            reset_seconds = max(0, int((reset_at - now).total_seconds()))
        else:
            reset_seconds = 0

        reset_label = _format_duration(reset_seconds)

        return {
            "name": name,
            "window_label": label,
            "tokens_used": tokens,
            "token_cap": cap,
            "percent_used": round(pct, 1),
            "resets_in_seconds": reset_seconds,
            "resets_in_label": reset_label,
            "query_count": queries,
            "window_start_iso": cutoff.isoformat(),
            "window_end_iso": now.isoformat(),
        }

    session_info = _compute_window(
        "session",
        f"{session_window_hours}hr",
        session_tokens,
        session_queries,
        session_cap,
        session_cutoff,
        session_oldest_ts,
        timedelta(hours=session_window_hours),
    )

    weekly_info = _compute_window(
        "weekly",
        f"{weekly_window_days} day",
        weekly_tokens,
        weekly_queries,
        weekly_cap,
        weekly_cutoff,
        weekly_oldest_ts,
        timedelta(days=weekly_window_days),
    )

    return {
        "session": session_info,
        "weekly": weekly_info,
        "subscription_type": _read_subscription_type(claude_dir),
        "computed_at": now.isoformat(),
    }


def _format_duration(seconds: int) -> str:
    """Format seconds into a human-readable duration like '2h 13m' or '6d 4h'."""
    if seconds <= 0:
        return "now"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def _read_subscription_type(claude_dir: Path) -> str:
    """Read subscription type from .credentials.json."""
    creds_path = claude_dir / ".credentials.json"
    try:
        with open(creds_path, "r", encoding="utf-8") as f:
            creds = json.load(f)
        return creds.get("claudeAiOauth", {}).get("subscriptionType", "unknown")
    except (OSError, json.JSONDecodeError):
        return "unknown"


def _iter_file_lines(path: Path):
    """Yield non-empty lines from a file, silently handling errors."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield line
    except OSError:
        return
