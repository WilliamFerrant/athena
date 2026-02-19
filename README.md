# Athena

A multi-agent system for full-stack freelance development powered by Claude Code CLI and orchestrated with LangGraph.

## How It Works

The system has four AI agents, each with a specialized role, personality, and simulated needs (like characters in The Sims). A central **Manager** receives your task, breaks it into subtasks, assigns them to the right specialist, reviews the output, and synthesizes a final deliverable.

### The Agents

| Agent | Role | Personality |
|-------|------|-------------|
| **Manager** | Decomposes tasks, delegates, reviews output, synthesizes final result | Collaborative, pragmatic, mentoring |
| **Frontend** | React/Next.js, TypeScript, CSS, accessibility, responsive design | Design-minded, creative, perfectionist |
| **Backend** | APIs, databases, security, server logic, infrastructure | Methodical, security-first, performance-obsessed |
| **Tester** | Unit tests, integration tests, E2E, coverage analysis | Test-driven, methodical, challenger |

### The Pipeline

When you submit a task, it flows through this pipeline:

```
Intake -> Planning -> Executing -> Reviewing -> Synthesizing -> Done
                         ^             |
                         |__ revise ___|
```

1. **Intake** - Task received and validated
2. **Planning** - Manager decomposes the task into subtasks with dependencies
3. **Executing** - Specialist agents work on their assigned subtasks
4. **Reviewing** - Manager reviews each output (approve / revise / redo)
5. **Synthesizing** - Manager combines all approved outputs into a final deliverable

If the Manager requests revisions, the subtask loops back to the executing step (up to 2 revision rounds).

### Sims-Like Drive System

Each agent has four simulated needs that affect their behavior:

- **Energy** (depleted by work, restored by rest)
- **Focus** (depleted by context-switching)
- **Morale** (boosted by success, drained by failures)
- **Knowledge** (boosted by learning, decays slowly over time)

These drives influence the agent's system prompt. An exhausted agent gets instructions to be concise; a demoralized agent is encouraged to ask for help.

### Memory (mem0.ai)

Each agent has persistent memory powered by [mem0.ai](https://mem0.ai). Memories are:
- **Stored** after meaningful conversations
- **Searched** before each new task for relevant context
- **Pruned** automatically when they exceed a window (default 200 per agent)

This means agents learn from past sessions and get better at your specific projects over time.

### LLM Backend

The system uses **Claude Code CLI** (`claude -p`) as the LLM backend. This runs on your existing Claude Code subscription -- **no Anthropic API key required, no additional cost**.

## Setup

### Prerequisites

- Python 3.10+
- [Claude Code](https://claude.ai/code) installed and authenticated (`claude` on PATH)
- (Optional) [mem0.ai](https://mem0.ai) account for persistent memory

### Install

```bash
git clone <your-repo-url> ai-companion-claude
cd ai-companion-claude
pip install -e ".[dev]"
```

### Configure

```bash
cp .env.example .env
```

Edit `.env` with your settings:

```env
# Required: Claude Code must be installed and on PATH
# CLAUDE_CLI_PATH=claude    # default

# Optional: enables persistent agent memory
MEM0_API_KEY=m0-your-key-here

# Model routing (passed to claude --model)
DEFAULT_MODEL=sonnet
MANAGER_MODEL=sonnet

# Rate guard
DAILY_CALL_LIMIT=200
```

### Run

**Web dashboard** (recommended):
```bash
python -m src.main serve
# Open http://localhost:8000
```

**CLI one-shot**:
```bash
python -m src.main run "Build a REST API for a todo app with auth"
```

## Dashboard

The web dashboard at `http://localhost:8000` has five tabs:

### Usage Tracker
Shows total CLI calls, per-agent breakdown, and remaining daily quota. Since the system uses Claude Code CLI (not the API), cost is always $0.

### Agents
Displays the four available agents with their roles and types.

### Memory
- **Load** all stored memories for any agent
- **Add** new memories manually
- **Search** memories semantically
- **Clear** an agent's memory

### Orchestrator
- Visual pipeline showing the current workflow step
- Submit full tasks to the multi-agent pipeline
- View the Manager's plan and final synthesized output

### Chat
Direct 1-on-1 chat with any agent. Select an agent from the dropdown and start messaging. Each agent responds with its specialized knowledge and personality.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web dashboard |
| `GET` | `/api/status` | System status + usage stats |
| `GET` | `/api/agents` | List available agents |
| `POST` | `/api/task` | Submit task to full pipeline |
| `POST` | `/api/chat` | Chat with a specific agent |
| `POST` | `/api/budget/reset` | Reset daily call counter |
| `GET` | `/api/memory/{agent_id}` | Get agent's memories |
| `POST` | `/api/memory/add` | Store a new memory |
| `POST` | `/api/memory/search` | Search memories |
| `DELETE` | `/api/memory/{agent_id}` | Clear agent's memories |
| `GET` | `/api/runner/status` | Runner online/offline status |
| `POST` | `/api/runner/cmd` | Execute command on runner |
| `GET` | `/api/runner/git/status` | Git status via runner |
| `GET` | `/api/runner/git/diff` | Git diff via runner |
| `POST` | `/api/runner/claude/run` | Run Claude CLI on runner |
| `POST` | `/api/runner/git/push-pr` | Create branch + PR via runner |
| `GET` | `/api/runner/dev-state/{project_id}` | Combined dev state (or offline) |
| `GET` | `/docs` | Swagger UI (auto-generated) |

## Local Runner + Reverse SSH Tunnel

The **Local Runner** executes Claude CLI and project commands on your Windows PC while the **Control Plane** (dashboard) runs on a Hetzner VPS. They communicate through a reverse SSH tunnel — no ports are publicly exposed.

### Architecture

```
┌────────────────────────┐       SSH tunnel       ┌────────────────────────┐
│    Windows PC (home)   │ ────────────────────── │  Hetzner VPS (cloud)   │
│                        │                         │                        │
│  companion-runner      │   reverse tunnel:       │  Control Plane (8000)  │
│  ↳ 127.0.0.1:7777     │   VPS:17777 → PC:7777  │  ↳ /api/runner/*       │
│                        │                         │    → 127.0.0.1:17777   │
│  Claude CLI (Max plan) │                         │                        │
│  Project files (SSD)   │                         │  Dashboard UI          │
│  Git, npm, etc.        │                         │  Health checks (prod)  │
└────────────────────────┘                         └────────────────────────┘
```

### Setup

#### 1. Configure the Runner (Windows PC)

```bash
# In the ai-companion-claude repo directory
pip install -e ".[dev]"
```

Create/edit `.env`:
```env
RUNNER_TOKEN=your-secret-shared-token
RUNNER_HOST=127.0.0.1
RUNNER_PORT=7777
RUNNER_PROJECTS_FILE=projects.yaml
```

Start the runner:
```bash
companion-runner
# Or: python -m src.runner.main
```

#### 2. Open the Reverse SSH Tunnel

```bash
ssh -N -R 17777:127.0.0.1:7777 root@<HETZNER_IP>
```

This maps `localhost:17777` on the VPS to `localhost:7777` on your Windows PC.

**Keep-alive tips:**
- Add to `~/.ssh/config`:
  ```
  Host hetzner
      HostName <HETZNER_IP>
      User root
      ServerAliveInterval 30
      ServerAliveCountMax 3
  ```
- On Windows, use a PowerShell script or task scheduler to auto-reconnect
- The tunnel drops when your PC sleeps/shuts down — this is expected and the system handles it gracefully

#### 3. Configure the Control Plane (Hetzner VPS)

In `.env` on the VPS:
```env
RUNNER_BASE_URL=http://127.0.0.1:17777
RUNNER_TOKEN=your-secret-shared-token
RUNNER_POLL_INTERVAL=10
```

The control plane will automatically poll the runner every 10 seconds. When the tunnel is down, the dashboard shows "Runner: Offline".

### Runner API Contract

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Runner health check |
| `POST` | `/cmd` | Execute a command in project dir |
| `GET` | `/git/status?projectId=...` | Git branch, last commit, dirty files |
| `GET` | `/git/diff?projectId=...` | Unified diff (413 if too large) |
| `POST` | `/claude/run` | Run Claude CLI in project dir |
| `POST` | `/git/push-pr` | Commit + push branch + create PR |

All requests require `X-Runner-Token` header (when `RUNNER_TOKEN` is set).

### Safety Rules

The runner enforces these safety rules:
- **Never push to `main`/`master`** — always use feature branches
- **Never merge** — use GitHub PR review workflow
- **Block destructive commands** — `rm -rf /`, `format`, etc.
- **Block deploy commands** — must deploy manually or via CI/CD
- **Diff size limit** — returns 413 if diff exceeds 500KB

### Requirements

- **Python 3.10+** on both Windows PC and Hetzner VPS
- **Claude Code CLI** installed and authenticated on Windows PC (`claude` on PATH)
- **Git** on Windows PC
- **SSH access** to Hetzner VPS
- **(Optional) GitHub CLI** (`gh`) on Windows PC for automatic PR creation

## Testing

```bash
# All tests
python -m pytest tests/ -v

# Single file
python -m pytest tests/test_agents.py -v

# Single test
python -m pytest tests/test_agents.py::TestDriveSystem::test_tick_decays_drives -v

# With coverage
python -m pytest tests/ --cov=src --cov-report=term-missing
```

## Deployment (Hetzner)

For production deployment on a Hetzner Cloud VPS:

```bash
# On a fresh Ubuntu 24.04 Hetzner server (CX22 or higher):
ssh root@your-server-ip

# Copy project files
scp -r ./* root@your-server-ip:/opt/ai-companion/

# Run setup (installs Docker, configures firewall, SSL, launches app)
DOMAIN=api.yourdomain.com EMAIL=you@email.com bash /opt/ai-companion/deploy/hetzner/setup.sh
```

The setup script handles:
- Docker + Docker Compose installation
- UFW firewall (SSH, 80, 443)
- Let's Encrypt SSL certificate + auto-renewal
- Nginx reverse proxy
- Container orchestration

After setup, edit `/opt/ai-companion/.env` with your keys and restart:
```bash
docker compose -f deploy/hetzner/docker-compose.prod.yml restart
```

## Project Structure

```
src/
  main.py                 # Entry point (serve | run)
  config.py               # Settings from .env
  token_tracker/tracker.py # Claude CLI wrapper + usage tracking
  memory/mem0_client.py   # mem0.ai revolving memory
  agents/
    base.py               # BaseAgent (LLM + personality + drives + memory)
    manager.py            # Task decomposition, review, synthesis
    frontend.py           # Frontend specialist
    backend.py            # Backend specialist
    tester.py             # Testing specialist
    sims/
      personality.py      # 15 trait types + preset profiles
      drives.py           # Energy/Focus/Morale/Knowledge system
  orchestrator/
    state.py              # LangGraph TypedDict state
    graph.py              # StateGraph: intake->plan->execute->review->synthesize
  api/
    server.py             # FastAPI + static file serving
    routes.py             # All REST endpoints
    runner_routes.py      # Control plane → runner proxy endpoints
    health_routes.py      # Project health + SSE stream
  runner/
    main.py               # companion-runner entry point
    app.py                # Runner FastAPI app + auth middleware
    config.py             # Runner-specific settings
    endpoints.py          # /health, /cmd, /git/*, /claude/run, /git/push-pr
    safety.py             # Command blocklist + branch protection
  runner_connector/
    client.py             # httpx client for runner API
    poller.py             # Background runner health poller
    models.py             # Pydantic request/response models
  projects/
    registry.py           # Project registry (projects.yaml)
  static/
    index.html            # Dashboard GUI
tests/                    # pytest tests
deploy/hetzner/           # Docker + nginx + setup script
```

