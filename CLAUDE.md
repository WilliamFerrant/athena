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
│   ├── tracker.py       # Calls `claude -p` via subprocess; tracks per-agent calls/chars
│   └── session_parser.py # Parses ~/.claude JSONL session files for real usage data
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
├── health/
│   ├── engine.py        # HealthStore: SQLite-backed health check storage
│   └── scheduler.py     # HealthScheduler: runs periodic project health checks
├── projects/
│   └── registry.py      # ProjectRegistry: loads projects.yaml, maps IDs → config
├── tasks/
│   └── store.py         # TaskStore: SQLite kanban (backlog→planned→in-progress→review→done)
├── runner/
│   └── endpoints.py     # Local runner FastAPI endpoints (cmd, git, claude, push-pr)
├── runner_connector/
│   ├── client.py        # RunnerClient: httpx client for runner via reverse SSH tunnel
│   ├── poller.py        # RunnerPoller: polls runner health every 10s
│   └── models.py        # Pydantic models for runner requests/responses
├── static/
│   ├── index.html       # Bridge dashboard (main landing page)
│   └── workshop.html    # Workshop Mode (project management + floating windows)
└── api/
    ├── server.py        # FastAPI app factory with lifespan + CORS
    ├── routes.py        # Chat, task pipeline, usage, memory, streaming endpoints
    ├── health_routes.py # Project health + SSE streaming
    ├── runner_routes.py # Runner proxy endpoints (cmd, git, claude, push-pr)
    └── task_routes.py   # Task board CRUD + kanban board view
```

## Key Design Decisions

- **LLM backend is Claude Code CLI** (`claude -p`), not the Anthropic API. No API key or per-token cost. The `TokenTracker` invokes `subprocess.run(["claude", "-p", ...])`.
- **WorkflowState is a TypedDict** (via LangGraph's MessagesState). All graph node functions receive plain dicts — always use `state["key"]` syntax, not `state.key`.
- **SubtaskResult is a dataclass** stored inside the state dict; it's the only non-dict state object.
- **Manager parses JSON from CLI text output** (no tool_use). System prompt instructs JSON format; `decompose_task()` extracts it with `find("{")` / `rfind("}")`.
- **Drives decay on every `agent.chat()` call** via `self.drives.tick()`. Events like success/failure/rest modify drive levels.
- **Memory is optional**: agents work without mem0 keys configured; `_maybe_memory()` catches init failures.
- **Config uses `extra="ignore"`** so old `.env` fields (like `ANTHROPIC_API_KEY`) don't cause validation errors.
- **Two dashboards**: `/` serves Bridge (index.html), `/workshop` serves Workshop Mode (workshop.html). Workshop is the project management IDE; Bridge is the system overview.
- **Runner architecture**: Local runner (port 7777) connects to VPS via reverse SSH tunnel (`-R 17777:localhost:7777`). The RunnerPoller on the VPS polls the tunnel port every 10s. All runner commands go through `RunnerClient` → tunnel → local runner.
- **Task Board uses SQLite**: `data/tasks.db` with WAL mode. 5 kanban columns, priority ordering, per-project filtering.
- **Streaming via SSE**: `POST /api/chat/stream`, `POST /api/orchestrator/stream`, `POST /api/runner/cmd/stream`. Uses `text/event-stream` with named events.
- **Safety rules**: Runner blocks `git push` to main/master. PR flow enforced. Workshop never auto-pushes.

## Workshop Mode (`/workshop`)

The Workshop is a full project management IDE served at `/workshop`:

- **Left sidebar**: Project list with health dots + dev-state indicators, health cards with SSE live updates, activity feed
- **Center**: File tree (changed files only from `git status`), floating window area with DiffViewer/CommitViewer/TerminalOutput
- **Right**: Project-scoped chat panel with agent selector (Manager/Frontend/Backend/Tester), streaming responses, action buttons (Launch Dev, Stage All, Generate Commit, Run Tests)
- **Task Board**: Full-screen overlay with 5-column kanban (backlog→done), task cards with agent/priority/autopilot, inline create/edit

## Runner & Tunnel

```bash
# On local machine: start the runner
python -m src.runner.endpoints  # listens on 0.0.0.0:7777

# Open reverse SSH tunnel to VPS
ssh -R 17777:localhost:7777 user@vps-ip -N
```

The VPS `RunnerPoller` automatically detects the runner via the tunnel.
Runner endpoints: `/health`, `/cmd`, `/git/status`, `/git/diff`, `/claude/run`, `/git/push-pr`.

## Environment

Optional `.env` file (see `.env.example`):
- `CLAUDE_CLI_PATH` — path to `claude` binary (default: `claude` on PATH)
- `MEM0_API_KEY` — optional, enables persistent memory
- `DAILY_CALL_LIMIT` — max CLI calls per day (default 200)
