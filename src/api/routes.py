"""API routes for interacting with the multi-agent system."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.memory.mem0_client import AgentMemory
from src.orchestrator.graph import run_task
from src.token_tracker.tracker import TokenTracker

logger = logging.getLogger(__name__)

router = APIRouter()

# Server-side plan cache: plan_id → {plan, subtasks, project_id}
# Plans expire after 30 minutes
_pending_plans: dict[str, dict[str, Any]] = {}


# -- Request/Response models ---------------------------------------------------


# -- Helpers for plan detection + auto-execution --------------------------------

def _try_extract_plan(text: str) -> dict[str, Any] | None:
    """Try to extract a JSON plan with subtasks from agent response text."""
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(text[start:end])
            if (
                "subtasks" in data
                and isinstance(data["subtasks"], list)
                and len(data["subtasks"]) > 0
            ):
                return data
    except (ValueError, KeyError, json.JSONDecodeError):
        pass
    return None


def _detect_project_id(plan_data: dict[str, Any], registry: Any) -> str:
    """Detect which project a plan targets. Defaults to ai-companion (self-edit)."""
    if not registry:
        return "ai-companion"
    plan_text = (plan_data.get("plan", "") + " ").lower()
    # Self-edit indicators take precedence
    self_keywords = ["projects.yaml", "mon dashboard", "my dashboard", "my config", "ma config", "athena"]
    if any(kw in plan_text for kw in self_keywords):
        return "ai-companion"
    for st in plan_data.get("subtasks", []):
        desc = st.get("description", "").lower()
        if any(kw in desc for kw in self_keywords):
            return "ai-companion"
    # Check for explicit project names
    for pid in registry.list_ids():
        if pid in plan_text:
            return pid
    for st in plan_data.get("subtasks", []):
        desc = st.get("description", "").lower()
        for pid in registry.list_ids():
            if pid in desc:
                return pid
    return "ai-companion"


class TaskRequest(BaseModel):
    task: str
    use_memory: bool = True


class TaskResponse(BaseModel):
    plan: str
    final_output: str
    subtask_count: int
    token_summary: dict[str, Any]


class ChatRequest(BaseModel):
    agent: str
    message: str
    task_context: str = ""
    project_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    agent_id: str
    token_summary: dict[str, Any]


class MemoryAddRequest(BaseModel):
    agent_id: str
    content: str


class MemorySearchRequest(BaseModel):
    agent_id: str
    query: str
    limit: int = 10


# -- Task endpoints ------------------------------------------------------------


@router.post("/task", response_model=TaskResponse)
def submit_task(req: TaskRequest, request: Request) -> TaskResponse:
    """Submit a high-level task to the multi-agent system."""
    tracker: TokenTracker = request.app.state.tracker

    if tracker.is_over_budget:
        raise HTTPException(status_code=429, detail="Daily call limit exhausted")

    result = run_task(req.task, tracker=tracker, use_memory=req.use_memory)

    return TaskResponse(
        plan=result.get("plan", ""),
        final_output=result.get("final_output", ""),
        subtask_count=len(result.get("subtasks", [])),
        token_summary=tracker.global_summary(),
    )


# -- Chat endpoints ------------------------------------------------------------


@router.get("/chat/greet")
def athena_greet(request: Request) -> dict[str, str]:
    """Get Athena's greeting message, personalized via her global memory."""
    agents = getattr(request.app.state, "agents", {})
    manager = agents.get("manager")
    if not manager or not hasattr(manager, "greet_user"):
        return {"greeting": "Hello! I'm Athena. How can I help?"}
    return {"greeting": manager.greet_user()}


class CreateProjectFromChatRequest(BaseModel):
    message: str  # natural-language project description from the user
    # When the user answers Athena's follow-up questions, pass the original
    # extracted details here so we skip re-extraction and go straight to creation.
    confirmed_details: dict[str, Any] | None = None


# Fields Athena will always ask about if missing (never silently skip)
_CRITICAL_FIELDS = {"git_remote", "health_url"}


@router.post("/chat/create-project")
def chat_create_project(req: CreateProjectFromChatRequest, request: Request) -> dict[str, Any]:
    """Let Athena parse a natural-language message and register a new project.

    Flow:
    1. First call (no confirmed_details): Athena extracts what she can.
       - If critical fields (git_remote, health_url) are missing → returns
         ``{"status": "needs_info", "questions": "...", "partial": {...}}``
         so the frontend can show Athena's question to the user.
       - If everything is present → creates immediately.
    2. Second call (confirmed_details set): skip extraction, create directly
       with the completed data from the user's answer.
    """
    agents = getattr(request.app.state, "agents", {})
    manager = agents.get("manager")
    if not manager or not hasattr(manager, "extract_project_details"):
        raise HTTPException(status_code=503, detail="Manager agent unavailable")

    # -- Step 2: user answered Athena's questions, create directly -------------
    if req.confirmed_details:
        details = req.confirmed_details
    else:
        # -- Step 1: extract from natural language -----------------------------
        details = manager.extract_project_details(req.message)
        if not details:
            return {"detected": False, "message": "No project registration intent detected"}

        # Check for missing critical fields Athena couldn't infer
        missing: list[str] = details.get("missing") or []
        critical_missing = [f for f in missing if f in _CRITICAL_FIELDS]
        if critical_missing:
            return {
                "detected": True,
                "status": "needs_info",
                "missing": critical_missing,
                "questions": details.get("questions", "Can you provide the missing details?"),
                "partial": details,  # frontend sends this back as confirmed_details after user answers
            }

    # -- Create the project ---------------------------------------------------
    from src.api.health_routes import ProjectUpsertRequest, _req_to_entry, _read_yaml, _write_yaml

    registry = request.app.state.registry

    pid = details.get("id") or ""
    if not pid:
        import re
        pid = re.sub(r"[^a-z0-9]+", "-", details.get("name", "project").lower()).strip("-")
        details["id"] = pid

    if registry.get(pid):
        raise HTTPException(status_code=409, detail=f"Project '{pid}' already exists")

    # Build upsert request (only known fields; ignore internal keys like "missing"/"questions")
    known_fields = ProjectUpsertRequest.model_fields
    upsert_data = {k: v for k, v in details.items() if k in known_fields}
    upsert_data["id"] = pid
    # Auto-map repo_path → path_windows when it looks like a Windows absolute path
    repo = upsert_data.get("repo_path", "")
    if repo and not upsert_data.get("path_windows") and (repo.startswith("C:") or repo.startswith("D:")):
        upsert_data["path_windows"] = repo
    upsert_req = ProjectUpsertRequest(**upsert_data)

    yaml_data = _read_yaml()
    entry = _req_to_entry(upsert_req)

    # Append TLS + DNS checks if hostnames were extracted
    tls_host = details.get("tls_hostname") or ""
    dns_host = details.get("dns_hostname") or tls_host
    extra_checks: list[dict] = entry.get("health_checks") or []
    if tls_host:
        extra_checks.append({"id": "tls-cert", "type": "tls", "hostname": tls_host, "warn_days_before": 14, "interval_seconds": 3600})
    if dns_host:
        extra_checks.append({"id": "dns-resolve", "type": "dns", "hostname": dns_host, "interval_seconds": 300})
    if extra_checks:
        entry["health_checks"] = extra_checks

    yaml_data.setdefault("projects", []).append(entry)
    _write_yaml(yaml_data)
    registry.reload()

    logger.info("Athena registered new project via chat: %s", pid)
    return {"detected": True, "status": "created", "id": pid, "name": details.get("name", pid)}


@router.post("/chat", response_model=ChatResponse)
def chat_with_agent(req: ChatRequest, request: Request) -> ChatResponse:
    """Chat directly with a specific agent."""
    tracker: TokenTracker = request.app.state.tracker

    # Record user activity for heartbeat idle timer
    heartbeat = getattr(request.app.state, "heartbeat", None)
    if heartbeat:
        heartbeat.record_user_activity()

    if tracker.is_over_budget:
        raise HTTPException(status_code=429, detail="Daily call limit exhausted")

    agents = getattr(request.app.state, "agents", {})
    agent = agents.get(req.agent)
    if not agent:
        raise HTTPException(status_code=400, detail=f"Unknown agent: {req.agent}")

    # Scope memory to project when project_id is provided
    if req.project_id:
        try:
            agent.project_memory = AgentMemory(agent_id=req.agent, project_id=req.project_id)
        except Exception:
            agent.project_memory = None
    else:
        agent.project_memory = None

    response = agent.chat(req.message, task_context=req.task_context or req.project_id or "")

    return ChatResponse(
        response=response,
        agent_id=req.agent,
        token_summary=tracker.agent_summary(req.agent),
    )


# -- Status endpoints ----------------------------------------------------------


@router.get("/version")
def get_version() -> dict[str, Any]:
    """Return build SHA for the running instance."""
    import subprocess

    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        sha = "unknown"
    return {"sha": sha}


@router.get("/status")
def system_status(request: Request) -> dict[str, Any]:
    """Get system-wide status including token usage."""
    tracker: TokenTracker = request.app.state.tracker
    return {
        "status": "ok",
        "token_usage": tracker.global_summary(),
    }


@router.get("/agents")
def list_agents() -> dict[str, Any]:
    """List available agents and their capabilities."""
    return {
        "agents": [
            {
                "id": "manager",
                "type": "manager",
                "description": "Central coordinator -- decomposes tasks, delegates, reviews",
            },
            {
                "id": "frontend",
                "type": "frontend",
                "description": "React/Next.js, TypeScript, CSS, accessibility",
            },
            {
                "id": "backend",
                "type": "backend",
                "description": "Python/Node APIs, databases, security, infrastructure",
            },
            {
                "id": "tester",
                "type": "tester",
                "description": "pytest, Vitest, E2E, coverage analysis",
            },
        ]
    }


@router.post("/budget/reset")
def reset_budget(request: Request) -> dict[str, str]:
    """Reset the daily call counter."""
    tracker: TokenTracker = request.app.state.tracker
    tracker.reset_daily()
    return {"status": "budget reset"}


# -- Agent drives endpoints ----------------------------------------------------


class DrivesOptimizeRequest(BaseModel):
    n_episodes: int = 5


@router.get("/drives/status")
def get_drives_status() -> dict[str, Any]:
    """Return drive levels for all agents with fresh DriveSystem instances.

    Note: these are representative starting states; actual per-task drive
    levels live inside each orchestrator run's agent instances.
    """
    from src.agents.sims.drives import DriveSystem

    agents = ["manager", "frontend", "backend", "tester"]
    return {
        "agents": [
            {"agent_id": ag, "drives": DriveSystem().state.to_dict()}
            for ag in agents
        ]
    }


@router.post("/drives/optimize")
def optimize_drives(req: DrivesOptimizeRequest) -> dict[str, Any]:
    """Run gymnasium RL episodes to find optimal drive recovery sequences.

    Returns the best episode reward and event sequence across *n_episodes*
    random-policy episodes.  Requires the ``gymnasium`` package.
    """
    from src.agents.sims.drives import DriveSystem

    ds = DriveSystem()
    result = ds.optimize_via_rl(n_episodes=max(1, min(req.n_episodes, 20)))
    return result


class PPOOptimizeRequest(BaseModel):
    n_episodes: int = 5
    timesteps: int = 10_000
    force_retrain: bool = False


@router.post("/drives/optimize-ppo")
def optimize_drives_ppo_endpoint(req: PPOOptimizeRequest) -> dict[str, Any]:
    """Train a PPO policy and evaluate drive recovery strategies.

    Uses stable-baselines3 PPO if installed, falls back to random policy.
    The trained model is cached to data/rl_models/ for reuse.
    """
    from src.agents.sims.ppo_optimizer import optimize_drives_ppo

    return optimize_drives_ppo(
        n_episodes=max(1, min(req.n_episodes, 20)),
        timesteps=max(1000, min(req.timesteps, 100_000)),
        force_retrain=req.force_retrain,
    )


# -- Real token usage endpoints (from ~/.claude session data) ------------------


@router.get("/usage")
def get_real_usage(request: Request) -> dict[str, Any]:
    """Get complete real token usage parsed from Claude Code session files.

    Returns sessions, daily usage, model breakdown, top prompts,
    totals, and actionable insights — all from actual ~/.claude data.
    """
    tracker: TokenTracker = request.app.state.tracker
    return tracker.get_real_usage_dict()


@router.get("/usage/refresh")
def refresh_real_usage(request: Request) -> dict[str, Any]:
    """Force re-parse session files and return freshly computed data."""
    tracker: TokenTracker = request.app.state.tracker
    data = tracker.refresh_real_usage()
    return {
        "status": "refreshed",
        "total_sessions": data.get("totals", {}).get("total_sessions", 0),
        "total_tokens": data.get("totals", {}).get("total_tokens", 0),
    }


@router.get("/usage/sessions")
def get_sessions(request: Request) -> dict[str, Any]:
    """List all sessions sorted by token usage (highest first)."""
    tracker: TokenTracker = request.app.state.tracker
    data = tracker.get_real_usage_dict()
    return {"sessions": data.get("sessions", []), "totals": data.get("totals", {})}


@router.get("/usage/daily")
def get_daily_usage(request: Request) -> dict[str, Any]:
    """Daily token usage breakdown."""
    tracker: TokenTracker = request.app.state.tracker
    data = tracker.get_real_usage_dict()
    return {"daily_usage": data.get("daily_usage", [])}


@router.get("/usage/models")
def get_model_breakdown(request: Request) -> dict[str, Any]:
    """Token usage broken down by model."""
    tracker: TokenTracker = request.app.state.tracker
    data = tracker.get_real_usage_dict()
    return {"model_breakdown": data.get("model_breakdown", [])}


@router.get("/usage/top-prompts")
def get_top_prompts(request: Request) -> dict[str, Any]:
    """Top 20 most expensive prompts by token count."""
    tracker: TokenTracker = request.app.state.tracker
    data = tracker.get_real_usage_dict()
    return {"top_prompts": data.get("top_prompts", [])}


@router.get("/usage/insights")
def get_usage_insights(request: Request) -> dict[str, Any]:
    """Actionable insights about your Claude Code usage patterns."""
    tracker: TokenTracker = request.app.state.tracker
    data = tracker.get_real_usage_dict()
    return {"insights": data.get("insights", [])}


@router.get("/usage/limits")
def get_rate_limits(request: Request) -> dict[str, Any]:
    """Get current rate limit status (session 5hr + weekly 7d windows).

    Computes token usage within rolling time windows against estimated caps.
    Includes percentage used and time until the oldest activity exits the window.
    """
    tracker: TokenTracker = request.app.state.tracker
    return tracker.get_rate_limits()


# -- Memory endpoints ----------------------------------------------------------


@router.get("/memory/{agent_id}")
def get_agent_memories(agent_id: str) -> dict[str, Any]:
    """Get all memories for an agent."""
    try:
        mem = AgentMemory(agent_id=agent_id)
        return {
            "agent_id": agent_id,
            "memories": mem.get_all(),
            "stats": mem.stats(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memory error: {e}")


@router.post("/memory/add")
def add_memory(req: MemoryAddRequest) -> dict[str, Any]:
    """Add a memory for an agent."""
    try:
        mem = AgentMemory(agent_id=req.agent_id)
        result = mem.add(req.content)
        return {"status": "ok", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memory error: {e}")


@router.post("/memory/search")
def search_memories(req: MemorySearchRequest) -> dict[str, Any]:
    """Search an agent's memories."""
    try:
        mem = AgentMemory(agent_id=req.agent_id)
        results = mem.search(req.query, limit=req.limit)
        return {"agent_id": req.agent_id, "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memory error: {e}")


@router.delete("/memory/{agent_id}")
def clear_agent_memories(agent_id: str) -> dict[str, str]:
    """Clear all memories for an agent."""
    try:
        mem = AgentMemory(agent_id=agent_id)
        mem.clear()
        return {"status": f"memories cleared for {agent_id}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memory error: {e}")


@router.get("/memory/graph")
def get_memory_graph() -> dict[str, Any]:
    """Return the in-process knowledge graph as a JSON-serialisable dict.

    Useful for visualising memory connections in the dashboard.
    """
    try:
        from src.memory.graph_context import get_shared_graph
        graph = get_shared_graph()
        return {"graph": graph.to_dict(), "stats": graph.stats()}
    except ImportError:
        raise HTTPException(status_code=503, detail="networkx not installed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Graph error: {e}")


# -- Usage chart data ----------------------------------------------------------


@router.get("/usage/chart-data")
def get_chart_data(request: Request) -> dict[str, Any]:
    """Structured token usage data formatted for dashboard charts.

    Returns the last 14 days of daily usage, model breakdown, totals,
    actionable insights, and rate-limit status — all in chart-ready shape.
    """
    tracker: TokenTracker = request.app.state.tracker
    data = tracker.get_real_usage_dict()

    daily: list[dict[str, Any]] = data.get("daily_usage", [])
    models: list[dict[str, Any]] = data.get("model_breakdown", [])

    return {
        "daily_labels": [d.get("date", "") for d in daily[-14:]],
        "daily_input": [d.get("input_tokens", 0) for d in daily[-14:]],
        "daily_output": [d.get("output_tokens", 0) for d in daily[-14:]],
        "model_labels": [m.get("model", "unknown") for m in models],
        "model_tokens": [m.get("total_tokens", 0) for m in models],
        "totals": data.get("totals", {}),
        "insights": data.get("insights", []),
        "rate_limits": tracker.get_rate_limits(),
    }


# -- Streaming endpoints (SSE) ------------------------------------------------


class StreamChatRequest(BaseModel):
    agent: str
    message: str
    task_context: str = ""
    project_id: str | None = None


@router.post("/chat/stream")
async def stream_chat(req: StreamChatRequest, request: Request):
    """Stream agent response via SSE.

    Events:
      event: start    data: {"agent":"..."}
      event: chunk    data: {"text":"..."}
      event: done     data: {"response":"...","agent_id":"...","token_summary":{...}}
      event: error    data: {"detail":"..."}
    """
    tracker: TokenTracker = request.app.state.tracker

    if tracker.is_over_budget:
        raise HTTPException(status_code=429, detail="Daily call limit exhausted")

    agents = getattr(request.app.state, "agents", {})
    agent = agents.get(req.agent)
    if not agent:
        raise HTTPException(status_code=400, detail=f"Unknown agent: {req.agent}")

    # Scope memory to project when project_id is provided
    if req.project_id:
        try:
            agent.project_memory = AgentMemory(agent_id=req.agent, project_id=req.project_id)
        except Exception:
            agent.project_memory = None
    else:
        agent.project_memory = None

    async def event_generator():
        # Send start event
        yield f"event: start\ndata: {json.dumps({'agent': req.agent})}\n\n"

        full_text = ""
        try:
            # Build system prompt with memory context
            task_ctx = req.task_context or req.project_id or ""
            system = agent.system_prompt(task_ctx)

            # Append user message to persistent conversation history
            agent._conversation.append({"role": "user", "content": req.message})
            # Trim to sliding window to control token costs
            from src.agents.base import MAX_CONVERSATION_MESSAGES
            if len(agent._conversation) > MAX_CONVERSATION_MESSAGES:
                agent._conversation = agent._conversation[-MAX_CONVERSATION_MESSAGES:]
            agent.drives.tick(minutes_worked=0.5)

            # Use the agent's own LLM backend for streaming (OpenAI for
            # Manager, Claude CLI for specialists).  Falls back to the
            # global tracker when the agent has no custom backend.
            stream_backend = agent.llm_backend

            async for event in stream_backend.create_message_stream(
                agent_id=req.agent,
                model=agent.default_model,
                system=system,
                messages=agent._conversation,
            ):
                if event["type"] == "chunk":
                    chunk = event["data"]
                    full_text += chunk
                    yield f"event: chunk\ndata: {json.dumps({'text': chunk})}\n\n"
                elif event["type"] == "error":
                    yield f"event: error\ndata: {json.dumps({'detail': event['data']})}\n\n"
                    return
                elif event["type"] == "done":
                    full_text = event["data"]

            # Store assistant response in conversation history
            agent._conversation.append({"role": "assistant", "content": full_text})

            # Store in memory: prefer project_memory when set, else global memory
            target_memory = agent.project_memory or agent.memory
            if target_memory and full_text:
                try:
                    target_memory.add_conversation(agent._conversation[-2:])
                except Exception:
                    logger.debug("Memory storage failed for %s", req.agent)

            # Send done event with full response
            summary = agent.llm_backend.agent_summary(req.agent)
            yield f"event: done\ndata: {json.dumps({'response': full_text, 'agent_id': req.agent, 'token_summary': summary})}\n\n"

            # ── Plan detection: send plan_ready event for user approval ──
            bridge = getattr(request.app.state, "execution_bridge", None)
            if bridge and full_text:
                plan_data = _try_extract_plan(full_text)
                if plan_data and plan_data.get("subtasks"):
                    registry = getattr(request.app.state, "registry", None)
                    project_id = req.project_id or _detect_project_id(plan_data, registry)
                    subtasks = plan_data["subtasks"]
                    plan_text = plan_data.get("plan", "")
                    runner_online = bridge.is_runner_online()

                    # Cache the plan server-side for later execution
                    plan_id = f"plan-{int(time.time())}"
                    _pending_plans[plan_id] = {
                        "plan": plan_text,
                        "subtasks": subtasks,
                        "project_id": project_id,
                    }

                    yield f"event: plan_ready\ndata: {json.dumps({'plan_id': plan_id, 'project_id': project_id, 'plan': plan_text, 'subtasks': subtasks, 'runner_online': runner_online})}\n\n"

        except Exception as e:
            logger.exception("Stream chat error")
            yield f"event: error\ndata: {json.dumps({'detail': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class StreamTaskRequest(BaseModel):
    task: str
    use_memory: bool = True
    project_id: str | None = None
    token_budget: int = 50


@router.post("/orchestrator/stop")
def stop_orchestrator(request: Request) -> dict[str, str]:
    """Signal the currently running orchestrator to stop after the current subtask batch."""
    from src.orchestrator.run_state import stop_run

    run_id = getattr(request.app.state, "current_run_id", None)
    if run_id:
        stopped = stop_run(run_id)
        return {"status": "stop_requested" if stopped else "run_not_found", "run_id": run_id}
    return {"status": "no_active_run"}


@router.post("/orchestrator/stream")
async def stream_orchestrator(req: StreamTaskRequest, request: Request):
    """Stream orchestrator progress via SSE.

    Events:
      event: phase     data: {"phase":"planning|executing|reviewing|synthesizing","detail":"..."}
      event: subtask   data: {"agent":"...","task":"...","status":"running|done|error","result":"..."}
      event: done      data: {"plan":"...","final_output":"...","subtask_count":N,"token_summary":{...}}
      event: error     data: {"detail":"..."}
    """
    from src.orchestrator.run_state import start_run, end_run

    tracker: TokenTracker = request.app.state.tracker

    if tracker.is_over_budget:
        raise HTTPException(status_code=429, detail="Daily call limit exhausted")

    run_id, stop_event = start_run()
    request.app.state.current_run_id = run_id
    calls_at_start = tracker.global_summary().get("total_calls", 0)

    async def event_generator():
        yield f"event: phase\ndata: {json.dumps({'phase': 'starting', 'run_id': run_id, 'detail': 'Athena is preparing the task...'})}\n\n"

        try:
            # Run the task in a thread since it's synchronous
            loop = asyncio.get_event_loop()
            yield f"event: phase\ndata: {json.dumps({'phase': 'planning', 'detail': 'Athena is decomposing the task...'})}\n\n"

            result = await loop.run_in_executor(
                None,
                lambda: run_task(
                    req.task,
                    tracker=tracker,
                    use_memory=req.use_memory,
                    project_id=req.project_id,
                    stop_event=stop_event,
                    token_budget=req.token_budget,
                    calls_at_start=calls_at_start,
                ),
            )

            plan = result.get("plan", "")
            subtasks = result.get("subtasks", [])
            final_output = result.get("final_output", "")

            if plan:
                yield f"event: phase\ndata: {json.dumps({'phase': 'planned', 'detail': plan})}\n\n"

            for i, st in enumerate(subtasks):
                yield f"event: subtask\ndata: {json.dumps({'index': i, 'agent': st.get('agent', '?'), 'task': st.get('task', ''), 'status': 'done', 'result': st.get('result', '')[:500]})}\n\n"

            yield f"event: phase\ndata: {json.dumps({'phase': 'synthesizing', 'detail': 'Building final output...'})}\n\n"

            summary = tracker.global_summary()
            yield f"event: done\ndata: {json.dumps({'plan': plan, 'final_output': final_output, 'subtask_count': len(subtasks), 'token_summary': summary})}\n\n"

        except Exception as e:
            logger.exception("Stream orchestrator error")
            yield f"event: error\ndata: {json.dumps({'detail': str(e)})}\n\n"
        finally:
            end_run(run_id)
            if getattr(request.app.state, "current_run_id", None) == run_id:
                request.app.state.current_run_id = None

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

# -- Heartbeat / Autonomy endpoints -------------------------------------------


@router.get("/heartbeat/status")
def heartbeat_status(request: Request) -> dict[str, Any]:
    """Get the autonomous heartbeat scheduler status."""
    heartbeat = getattr(request.app.state, "heartbeat", None)
    if not heartbeat:
        return {"running": False, "error": "Heartbeat scheduler not initialized"}
    return heartbeat.status()


@router.post("/heartbeat/toggle")
def heartbeat_toggle(request: Request) -> dict[str, Any]:
    """Start or stop the heartbeat scheduler."""
    heartbeat = getattr(request.app.state, "heartbeat", None)
    if not heartbeat:
        raise HTTPException(status_code=503, detail="Heartbeat scheduler not initialized")

    import asyncio as _asyncio
    if heartbeat._running:
        _asyncio.ensure_future(heartbeat.stop())
        return {"running": False, "message": "Heartbeat stopped"}
    else:
        _asyncio.ensure_future(heartbeat.start())
        return {"running": True, "message": "Heartbeat started"}


@router.post("/heartbeat/poke")
def heartbeat_record_activity(request: Request) -> dict[str, str]:
    """Record user activity (resets the idle timer)."""
    heartbeat = getattr(request.app.state, "heartbeat", None)
    if heartbeat:
        heartbeat.record_user_activity()
    return {"status": "ok"}


# -- Memory curation endpoints ------------------------------------------------


@router.get("/memory/curation/{agent_id}")
def memory_curation_stats(agent_id: str, request: Request) -> dict[str, Any]:
    """Get memory curation statistics for an agent."""
    curator = getattr(request.app.state, "memory_curator", None)
    if not curator:
        return {"error": "Memory curator not initialized"}
    return curator.stats(agent_id)


@router.post("/memory/curate/{agent_id}")
def trigger_curation(agent_id: str, request: Request) -> dict[str, Any]:
    """Manually trigger memory curation for an agent's memories."""
    curator = getattr(request.app.state, "memory_curator", None)
    if not curator:
        raise HTTPException(status_code=503, detail="Memory curator not initialized")

    agents = getattr(request.app.state, "agents", {})
    agent = agents.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    memory = agent.project_memory or agent.memory
    if not memory:
        return {"curated": 0, "message": "No memory configured for this agent"}

    raw_memories = memory.get_all()
    curated = curator.categorize_memories(raw_memories, agent_id)
    return {
        "curated": len(curated),
        "stats": curator.stats(agent_id),
    }


@router.post("/memory/archive/{agent_id}")
def trigger_archive(agent_id: str, request: Request) -> dict[str, Any]:
    """Archive cold/unused memories for an agent."""
    curator = getattr(request.app.state, "memory_curator", None)
    if not curator:
        raise HTTPException(status_code=503, detail="Memory curator not initialized")
    archived = curator.archive_cold_memories(agent_id)
    return {"archived": archived, "stats": curator.stats(agent_id)}


@router.get("/memory/evolution/{memory_id}")
def memory_evolution_chain(memory_id: str, request: Request) -> dict[str, Any]:
    """Get the historical evolution chain of a memory."""
    curator = getattr(request.app.state, "memory_curator", None)
    if not curator:
        raise HTTPException(status_code=503, detail="Memory curator not initialized")
    chain = curator.get_evolution_chain(memory_id)
    return {"chain": [m.to_dict() for m in chain]}


# -- Execution Bridge ----------------------------------------------------------


class ExecutePlanRequest(BaseModel):
    plan_id: str = ""          # Look up a cached plan by ID
    project_id: str = "ai-companion"
    plan: str = ""
    subtasks: list[dict[str, Any]] = []
    auto_approve: bool = False


@router.post("/execute")
async def execute_plan(body: ExecutePlanRequest, request: Request) -> Any:
    """Execute a plan through the execution bridge (runner → Claude CLI).

    Returns SSE stream with real-time progress events:
      event: progress  data: {"phase":"...", "detail":"..."}
      event: done       data: {"success":true, "branch":"...", ...}
      event: error      data: {"detail":"..."}

    Accepts either:
    - plan_id: look up a previously detected plan from the chat stream
    - plan + subtasks: provide the plan directly
    """
    bridge = getattr(request.app.state, "execution_bridge", None)
    if not bridge:
        raise HTTPException(status_code=503, detail="Execution bridge not initialized")

    if not bridge.is_runner_online():
        raise HTTPException(status_code=503, detail="Runner is offline — start the runner and SSH tunnel first")

    # Resolve the plan: from cache or from request body
    project_id = body.project_id
    plan_text = body.plan
    subtasks = body.subtasks

    if body.plan_id and body.plan_id in _pending_plans:
        cached = _pending_plans.pop(body.plan_id)
        project_id = cached.get("project_id", project_id)
        plan_text = cached.get("plan", plan_text)
        subtasks = cached.get("subtasks", subtasks)
    elif body.plan_id:
        raise HTTPException(status_code=404, detail=f"Plan '{body.plan_id}' not found or expired")

    if not subtasks:
        raise HTTPException(status_code=400, detail="No subtasks to execute")

    import queue as queue_mod
    progress_queue: queue_mod.Queue = queue_mod.Queue()

    def on_progress(phase: str, detail: str = "") -> None:
        progress_queue.put({"phase": phase, "detail": detail})

    async def event_generator():
        # Start execution in a background thread
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(
            None,
            lambda: bridge.execute_plan(
                project_id=project_id,
                plan=plan_text,
                subtasks=subtasks,
                requested_by="web",
                auto_approve=body.auto_approve,
                on_progress=on_progress,
            ),
        )

        # Stream progress events while execution runs
        while not future.done():
            try:
                msg = progress_queue.get_nowait()
                yield f"event: progress\ndata: {json.dumps(msg)}\n\n"
            except queue_mod.Empty:
                pass
            await asyncio.sleep(0.5)

        # Drain remaining progress messages
        while not progress_queue.empty():
            try:
                msg = progress_queue.get_nowait()
                yield f"event: progress\ndata: {json.dumps(msg)}\n\n"
            except queue_mod.Empty:
                break

        # Get the result
        try:
            result = future.result()
            yield f"event: done\ndata: {json.dumps({'success': result.success, 'project_id': result.project_id, 'branch': result.branch, 'pr_url': result.pr_url, 'error': result.error, 'duration_ms': result.duration_ms, 'subtask_results': result.subtask_results})}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'detail': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/execute/status")
def execution_status(request: Request) -> dict[str, Any]:
    """Check if the execution bridge and runner are available."""
    bridge = getattr(request.app.state, "execution_bridge", None)
    if not bridge:
        return {"available": False, "reason": "Execution bridge not initialized"}
    online = bridge.is_runner_online()
    return {
        "available": online,
        "reason": "Runner online" if online else "Runner offline",
    }