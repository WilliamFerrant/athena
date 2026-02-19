# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multi-agent system for full-stack freelance development. A central **Manager agent** decomposes tasks and delegates to specialist sub-agents (**Frontend**, **Backend**, **Tester**), each with Sims-like personality traits and drive systems. Orchestrated via LangGraph. Uses **Claude Code CLI** (`claude -p`) as the LLM backend — no Anthropic API key needed.

## Commands

```bash
# Install
pip install -e ".[dev]"

# Run tests
python -m pytest tests/ -v

# Run a single test
python -m pytest tests/test_agents.py::TestDriveSystem::test_tick_decays_drives -v

# Start API server (port 8000)
python -m src.main serve

# Run a task via CLI
python -m src.main run "Build a landing page with auth"

# Lint
ruff check src/ tests/
```

## Architecture

```
src/
├── main.py              # CLI + server entry point (argparse → serve | run)
├── config.py            # Pydantic Settings from .env (extra=ignore for back-compat)
├── token_tracker/
│   └── tracker.py       # Calls `claude -p` via subprocess; tracks per-agent calls/chars
├── memory/
│   └── mem0_client.py   # Revolving memory via mem0.ai (per-agent namespace, auto-prune)
├── agents/
│   ├── base.py          # BaseAgent: LLM calls + personality + drives + memory
│   ├── manager.py       # ManagerAgent: decomposes tasks (JSON output), reviews, synthesizes
│   ├── frontend.py      # FrontendAgent: React/Next.js/CSS specialist
│   ├── backend.py       # BackendAgent: APIs/DB/security specialist
│   ├── tester.py        # TesterAgent: pytest/Vitest/E2E specialist
│   └── sims/
│       ├── personality.py  # Trait enum + weighted personality profiles
│       └── drives.py       # Energy/Focus/Morale/Knowledge needs system
├── orchestrator/
│   ├── state.py         # WorkflowState (LangGraph TypedDict) + SubtaskResult
│   └── graph.py         # LangGraph StateGraph: intake→plan→execute→review→synthesize
└── api/
    ├── server.py        # FastAPI app factory with CORS + root endpoint
    └── routes.py        # POST /api/task, POST /api/chat, GET /api/status, GET /api/agents
```

## Key Design Decisions

- **LLM backend is Claude Code CLI** (`claude -p`), not the Anthropic API. No API key or per-token cost. The `TokenTracker` invokes `subprocess.run(["claude", "-p", ...])`.
- **WorkflowState is a TypedDict** (via LangGraph's MessagesState). All graph node functions receive plain dicts — always use `state["key"]` syntax, not `state.key`.
- **SubtaskResult is a dataclass** stored inside the state dict; it's the only non-dict state object.
- **Manager parses JSON from CLI text output** (no tool_use). System prompt instructs JSON format; `decompose_task()` extracts it with `find("{")` / `rfind("}")`.
- **Drives decay on every `agent.chat()` call** via `self.drives.tick()`. Events like success/failure/rest modify drive levels.
- **Memory is optional**: agents work without mem0 keys configured; `_maybe_memory()` catches init failures.
- **Config uses `extra="ignore"`** so old `.env` fields (like `ANTHROPIC_API_KEY`) don't cause validation errors.

## Environment

Optional `.env` file (see `.env.example`):
- `CLAUDE_CLI_PATH` — path to `claude` binary (default: `claude` on PATH)
- `MEM0_API_KEY` — optional, enables persistent memory
- `DAILY_CALL_LIMIT` — max CLI calls per day (default 200)
