"""LangGraph orchestration graph for the multi-agent workflow.

Flow:
  intake -> planning -> executing -> reviewing -> synthesizing -> done
                            ^             |
                            |___ revise __|

The manager decomposes tasks, specialist agents execute subtasks in
**parallel** (ThreadPoolExecutor + asyncio), the manager reviews outputs,
and the cycle continues until all subtasks are approved.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from langgraph.graph import END, StateGraph

from src.agents.backend import BackendAgent
from src.agents.frontend import FrontendAgent
from src.agents.manager import ManagerAgent
from src.agents.tester import TesterAgent
from src.memory.mem0_client import AgentMemory
from src.orchestrator.state import SubtaskResult, WorkflowState
from src.token_tracker.tracker import TokenTracker

logger = logging.getLogger(__name__)

# Maximum workers for parallel subtask execution
_MAX_PARALLEL_WORKERS = 4


def _create_agents(
    tracker: TokenTracker,
    use_memory: bool = True,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Initialize the agent pool with optional per-project memory isolation."""

    def _maybe_memory(agent_id: str, project_scoped: bool = True) -> AgentMemory | None:
        if not use_memory:
            return None
        pid = project_id if project_scoped else None
        try:
            return AgentMemory(agent_id=agent_id, project_id=pid)
        except Exception:
            logger.warning("Memory unavailable for %s, continuing without", agent_id)
            return None

    # Athena (manager) gets both global memory (user profile) and per-project memory.
    # Sub-agents get only per-project memory (no cross-project leakage).
    return {
        "manager": ManagerAgent(
            agent_id="manager",
            tracker=tracker,
            memory=_maybe_memory("manager", project_scoped=False),
            project_memory=_maybe_memory("manager", project_scoped=True) if project_id else None,
        ),
        "frontend": FrontendAgent(
            agent_id="frontend",
            tracker=tracker,
            project_memory=_maybe_memory("frontend"),
        ),
        "backend": BackendAgent(
            agent_id="backend",
            tracker=tracker,
            project_memory=_maybe_memory("backend"),
        ),
        "tester": TesterAgent(
            agent_id="tester",
            tracker=tracker,
            project_memory=_maybe_memory("tester"),
        ),
    }


# -- Parallel execution helpers ------------------------------------------------


def _execute_single(
    subtask_id: str,
    result: SubtaskResult,
    agents: dict[str, Any],
) -> tuple[str, SubtaskResult]:
    """Execute one subtask synchronously — designed to run inside a thread."""
    agent = agents.get(result.agent_type)
    if not agent:
        result.output = f"Error: no agent of type '{result.agent_type}'"
        result.review_verdict = "redo"
        return subtask_id, result

    prompt = result.description
    if result.review_feedback and result.attempts > 0:
        prompt += f"\n\nPrevious feedback (revision requested):\n{result.review_feedback}"

    logger.info("Executing subtask %s with %s agent", subtask_id, result.agent_type)
    try:
        output = agent.chat(prompt, task_context=result.description)
        result.output = output
        result.attempts += 1
        agent.drives.record_success()
    except Exception as e:
        result.output = f"Error during execution: {e}"
        result.review_verdict = "redo"
        agent.drives.record_failure()
        logger.error("Subtask %s failed: %s", subtask_id, e)

    return subtask_id, result


def _execute_parallel_sync(
    ready_queue: list[str],
    results: dict[str, SubtaskResult],
    agents: dict[str, Any],
    stop_event: threading.Event | None = None,
    token_budget_remaining: int | None = None,
) -> dict[str, SubtaskResult]:
    """Run all ready subtasks in parallel using a ThreadPoolExecutor.

    This is the sync variant called from the synchronous ``executing_node``
    and from ``asyncio.run`` when no event loop is running.
    """
    if not ready_queue:
        return results

    # Check stop conditions before starting any work
    if stop_event and stop_event.is_set():
        logger.info("Orchestrator stop requested — skipping subtask batch")
        return results

    if token_budget_remaining is not None and token_budget_remaining <= 0:
        logger.info("Orchestrator token budget exhausted — skipping subtask batch")
        return results

    n_workers = min(_MAX_PARALLEL_WORKERS, len(ready_queue))
    updated = dict(results)

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {
            executor.submit(_execute_single, sid, updated[sid], agents): sid
            for sid in ready_queue
            if sid in updated
        }
        for future in as_completed(futures):
            try:
                sid, result = future.result()
                updated[sid] = result
            except Exception as exc:
                sid = futures[future]
                logger.error("Parallel future for subtask %s raised: %s", sid, exc)
                if sid in updated:
                    updated[sid].output = f"Error during parallel execution: {exc}"
                    updated[sid].review_verdict = "redo"

    return updated


async def _execute_parallel_async(
    ready_queue: list[str],
    results: dict[str, SubtaskResult],
    agents: dict[str, Any],
) -> dict[str, SubtaskResult]:
    """Async wrapper: schedules each subtask as a thread via run_in_executor."""
    if not ready_queue:
        return results

    loop = asyncio.get_event_loop()
    n_workers = min(_MAX_PARALLEL_WORKERS, len(ready_queue))
    updated = dict(results)

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        coros = [
            loop.run_in_executor(executor, _execute_single, sid, updated[sid], agents)
            for sid in ready_queue
            if sid in updated
        ]
        completed = await asyncio.gather(*coros, return_exceptions=True)

    for item in completed:
        if isinstance(item, Exception):
            logger.error("Async parallel execute error: %s", item)
            continue
        sid, result = item  # type: ignore[misc]
        updated[sid] = result

    return updated


# -- Node functions ------------------------------------------------------------


def intake_node(state: dict) -> dict[str, Any]:
    """Receive and validate the incoming task."""
    logger.info("Intake: received task of length %d", len(state["task"]))
    return {"phase": "planning"}


def planning_node(state: dict, agents: dict[str, Any]) -> dict[str, Any]:
    """Manager decomposes the task into subtasks."""
    manager: ManagerAgent = agents["manager"]
    plan_result = manager.decompose_task(
        f"Decompose this freelance development task into subtasks for the team:\n\n{state['task']}"
    )

    subtasks = plan_result.get("subtasks", [])
    plan = plan_result.get("plan", "")

    # Initialize results tracking
    results = {}
    for st in subtasks:
        results[st["id"]] = SubtaskResult(
            subtask_id=st["id"],
            agent_type=st["agent"],
            description=st["description"],
        )

    # Find initially ready tasks (no dependencies)
    ready = [st["id"] for st in subtasks if not st.get("depends_on")]

    logger.info("Planning complete: %d subtasks, %d ready", len(subtasks), len(ready))
    return {
        "phase": "executing",
        "plan": plan,
        "subtasks": subtasks,
        "results": results,
        "ready_queue": ready,
    }


def executing_node(
    state: dict,
    agents: dict[str, Any],
    stop_event: threading.Event | None = None,
    budget_fn: Any = None,
) -> dict[str, Any]:
    """Execute all ready subtasks in parallel via ThreadPoolExecutor."""
    results = dict(state["results"])
    ready_queue: list[str] = state["ready_queue"]

    if not ready_queue:
        return {"phase": "reviewing", "results": results, "ready_queue": []}

    budget_remaining = budget_fn() if budget_fn else None

    logger.info(
        "Executing %d subtask(s) in parallel: %s",
        len(ready_queue),
        ", ".join(ready_queue),
    )

    # Use sync parallel execution (works both in and outside an event loop)
    results = _execute_parallel_sync(
        ready_queue,
        results,
        agents,
        stop_event=stop_event,
        token_budget_remaining=budget_remaining,
    )

    return {"phase": "reviewing", "results": results, "ready_queue": []}


async def aexecuting_node(state: dict, agents: dict[str, Any]) -> dict[str, Any]:
    """Async version of executing_node — for use with app.ainvoke()."""
    results = dict(state["results"])
    ready_queue: list[str] = state["ready_queue"]

    if not ready_queue:
        return {"phase": "reviewing", "results": results, "ready_queue": []}

    logger.info(
        "Async-executing %d subtask(s) in parallel: %s",
        len(ready_queue),
        ", ".join(ready_queue),
    )

    results = await _execute_parallel_async(ready_queue, results, agents)
    return {"phase": "reviewing", "results": results, "ready_queue": []}


def reviewing_node(state: dict, agents: dict[str, Any]) -> dict[str, Any]:
    """Manager reviews all newly executed subtask outputs."""
    manager: ManagerAgent = agents["manager"]
    results = dict(state["results"])
    needs_revision: list[str] = []

    for subtask_id, result in results.items():
        if result.review_verdict != "pending" and result.output and not result.output.startswith("Error"):
            if result.review_verdict in ("approve",):
                continue

        if not result.output or result.output.startswith("Error"):
            if result.attempts < state["max_revisions"]:
                result.review_verdict = "redo"
                needs_revision.append(subtask_id)
            else:
                result.review_verdict = "approve"
            results[subtask_id] = result
            continue

        logger.info("Reviewing subtask %s", subtask_id)
        review = manager.review_output(result.description, result.output)

        result.review_verdict = review.get("verdict", "approve")
        result.review_feedback = review.get("feedback", "")
        result.review_score = review.get("score", 7)

        if result.review_verdict in ("revise", "redo") and result.attempts >= state["max_revisions"]:
            logger.info("Subtask %s exceeded max revisions, force-approving", subtask_id)
            result.review_verdict = "approve"

        if result.review_verdict in ("revise", "redo"):
            needs_revision.append(subtask_id)

        results[subtask_id] = result

    all_approved = all(r.review_verdict == "approve" for r in results.values())

    if all_approved:
        next_phase = "synthesizing"
        ready_queue: list[str] = []
    else:
        next_phase = "executing"
        approved_ids = {sid for sid, r in results.items() if r.review_verdict == "approve"}
        ready_queue = list(needs_revision)

        for st in state["subtasks"]:
            sid = st["id"]
            if sid in approved_ids or sid in ready_queue:
                continue
            deps = st.get("depends_on", [])
            if deps and all(d in approved_ids for d in deps):
                ready_queue.append(sid)

    return {"phase": next_phase, "results": results, "ready_queue": ready_queue}


def synthesizing_node(state: dict, agents: dict[str, Any]) -> dict[str, Any]:
    """Manager synthesizes the final deliverable from all agent outputs."""
    manager: ManagerAgent = agents["manager"]

    agent_outputs = {
        sid: result.output
        for sid, result in state["results"].items()
        if result.output and not result.output.startswith("Error")
    }

    final = manager.synthesize(state["task"], agent_outputs)
    logger.info("Synthesis complete: %d chars", len(final))
    return {"phase": "done", "final_output": final}


# -- Routing -------------------------------------------------------------------


def route_after_review(state: dict) -> str:
    if state["phase"] == "synthesizing":
        return "synthesize"
    return "execute"


# -- Graph construction --------------------------------------------------------


def build_graph(
    tracker: TokenTracker | None = None,
    use_memory: bool = True,
    project_id: str | None = None,
    stop_event: threading.Event | None = None,
    token_budget: int | None = None,
    calls_at_start: int = 0,
) -> StateGraph:
    """Build the LangGraph state graph for the multi-agent workflow."""
    tracker = tracker or TokenTracker()
    agents = _create_agents(tracker, use_memory=use_memory, project_id=project_id)

    graph = StateGraph(WorkflowState)

    def _budget_remaining() -> int | None:
        if token_budget is None:
            return None
        used = tracker.global_summary().get("total_calls", 0) - calls_at_start
        return token_budget - used

    # Add nodes (bind agents and stop controls via closures)
    graph.add_node("intake", intake_node)
    graph.add_node("plan", lambda s: planning_node(s, agents))
    graph.add_node(
        "execute",
        lambda s: executing_node(s, agents, stop_event=stop_event, budget_fn=_budget_remaining),
    )
    graph.add_node("review", lambda s: reviewing_node(s, agents))
    graph.add_node("synthesize", lambda s: synthesizing_node(s, agents))

    # Edges
    graph.set_entry_point("intake")
    graph.add_edge("intake", "plan")
    graph.add_edge("plan", "execute")
    graph.add_edge("execute", "review")
    graph.add_conditional_edges("review", route_after_review, {
        "execute": "execute",
        "synthesize": "synthesize",
    })
    graph.add_edge("synthesize", END)

    return graph


def _initial_state(task: str) -> dict[str, Any]:
    return {
        "task": task,
        "messages": [],
        "phase": "intake",
        "plan": "",
        "subtasks": [],
        "results": {},
        "ready_queue": [],
        "final_output": "",
        "errors": [],
        "max_revisions": 2,
    }


def run_task(
    task: str,
    tracker: TokenTracker | None = None,
    use_memory: bool = True,
    project_id: str | None = None,
    stop_event: threading.Event | None = None,
    token_budget: int | None = None,
    calls_at_start: int = 0,
) -> dict:
    """Convenience function: build graph and run a task through it (sync)."""
    graph = build_graph(
        tracker=tracker,
        use_memory=use_memory,
        project_id=project_id,
        stop_event=stop_event,
        token_budget=token_budget,
        calls_at_start=calls_at_start,
    )
    app = graph.compile()
    return app.invoke(_initial_state(task))


async def arun_task(
    task: str,
    tracker: TokenTracker | None = None,
    use_memory: bool = True,
    project_id: str | None = None,
    stop_event: threading.Event | None = None,
    token_budget: int | None = None,
    calls_at_start: int = 0,
) -> dict:
    """Async convenience function — uses ainvoke for native async execution."""
    graph = build_graph(
        tracker=tracker,
        use_memory=use_memory,
        project_id=project_id,
        stop_event=stop_event,
        token_budget=token_budget,
        calls_at_start=calls_at_start,
    )
    app = graph.compile()
    return await app.ainvoke(_initial_state(task))
