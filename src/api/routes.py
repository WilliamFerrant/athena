"""API routes for interacting with the multi-agent system."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.memory.mem0_client import AgentMemory
from src.orchestrator.graph import run_task
from src.token_tracker.tracker import TokenTracker
from src.token_tracker.session_parser import report_to_dict

logger = logging.getLogger(__name__)

router = APIRouter()


# -- Request/Response models ---------------------------------------------------


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


@router.post("/chat", response_model=ChatResponse)
def chat_with_agent(req: ChatRequest, request: Request) -> ChatResponse:
    """Chat directly with a specific agent."""
    tracker: TokenTracker = request.app.state.tracker

    if tracker.is_over_budget:
        raise HTTPException(status_code=429, detail="Daily call limit exhausted")

    from src.agents.backend import BackendAgent
    from src.agents.frontend import FrontendAgent
    from src.agents.manager import ManagerAgent
    from src.agents.tester import TesterAgent

    agent_classes = {
        "frontend": FrontendAgent,
        "backend": BackendAgent,
        "tester": TesterAgent,
        "manager": ManagerAgent,
    }

    agent_cls = agent_classes.get(req.agent)
    if not agent_cls:
        raise HTTPException(status_code=400, detail=f"Unknown agent: {req.agent}")

    agent = agent_cls(agent_id=req.agent, tracker=tracker)
    response = agent.chat(req.message, task_context=req.task_context)

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

    from src.agents.backend import BackendAgent
    from src.agents.frontend import FrontendAgent
    from src.agents.manager import ManagerAgent
    from src.agents.tester import TesterAgent

    agent_classes = {
        "frontend": FrontendAgent,
        "backend": BackendAgent,
        "tester": TesterAgent,
        "manager": ManagerAgent,
    }

    agent_cls = agent_classes.get(req.agent)
    if not agent_cls:
        raise HTTPException(status_code=400, detail=f"Unknown agent: {req.agent}")

    agent = agent_cls(agent_id=req.agent, tracker=tracker)

    async def event_generator():
        # Send start event
        yield f"event: start\ndata: {json.dumps({'agent': req.agent})}\n\n"

        full_text = ""
        try:
            system = agent.system_prompt(req.task_context)
            messages = [{"role": "user", "content": req.message}]
            agent.drives.tick(minutes_worked=0.5)

            async for event in tracker.create_message_stream(
                agent_id=req.agent,
                model=agent.default_model,
                system=system,
                messages=messages,
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

            # Send done event with full response
            summary = tracker.agent_summary(req.agent)
            yield f"event: done\ndata: {json.dumps({'response': full_text, 'agent_id': req.agent, 'token_summary': summary})}\n\n"

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


@router.post("/orchestrator/stream")
async def stream_orchestrator(req: StreamTaskRequest, request: Request):
    """Stream orchestrator progress via SSE.

    Events:
      event: phase     data: {"phase":"planning|executing|reviewing|synthesizing","detail":"..."}
      event: subtask   data: {"agent":"...","task":"...","status":"running|done|error","result":"..."}
      event: done      data: {"plan":"...","final_output":"...","subtask_count":N,"token_summary":{...}}
      event: error     data: {"detail":"..."}
    """
    tracker: TokenTracker = request.app.state.tracker

    if tracker.is_over_budget:
        raise HTTPException(status_code=429, detail="Daily call limit exhausted")

    async def event_generator():
        yield f"event: phase\ndata: {json.dumps({'phase': 'starting', 'detail': 'Submitting task to orchestrator'})}\n\n"

        try:
            # Run the task in a thread since it's synchronous
            loop = asyncio.get_event_loop()
            yield f"event: phase\ndata: {json.dumps({'phase': 'planning', 'detail': 'Manager is decomposing the task...'})}\n\n"

            result = await loop.run_in_executor(
                None,
                lambda: run_task(req.task, tracker=tracker, use_memory=req.use_memory),
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

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
