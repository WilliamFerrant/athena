"""Task board API routes — kanban CRUD + board view."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from src.tasks.store import COLUMNS, Task, TaskStore

logger = logging.getLogger(__name__)

task_router = APIRouter(prefix="/tasks", tags=["tasks"])


# ── Request models ───────────────────────────────────────────────────────

class CreateTaskBody(BaseModel):
    title: str
    description: str = ""
    column: str = "backlog"
    project_id: str | None = None
    agent: str | None = None
    priority: int = 0
    autopilot: bool = False


class UpdateTaskBody(BaseModel):
    title: str | None = None
    description: str | None = None
    column: str | None = None
    project_id: str | None = None
    agent: str | None = None
    priority: int | None = None
    autopilot: bool | None = None
    result: str | None = None


class MoveTaskBody(BaseModel):
    column: str


# ── Helper ───────────────────────────────────────────────────────────────

def _get_store(request: Request) -> TaskStore:
    return request.app.state.task_store  # type: ignore[no-any-return]


# ── Endpoints ────────────────────────────────────────────────────────────

@task_router.get("/board")
def get_board(project_id: str | None = None, request: Request = None) -> dict[str, Any]:
    """Get the full kanban board (columns → tasks)."""
    store = _get_store(request)
    return {
        "columns": list(COLUMNS),
        "board": store.board(project_id),
        "stats": store.stats(project_id),
    }


@task_router.get("/")
def list_tasks(
    project_id: str | None = None,
    column: str | None = None,
    request: Request = None,
) -> dict[str, Any]:
    """List tasks, optionally filtered by project and/or column."""
    store = _get_store(request)
    if column:
        tasks = store.list_by_column(column, project_id)
    else:
        tasks = store.list_all(project_id)
    return {"tasks": [t.to_dict() for t in tasks], "count": len(tasks)}


@task_router.post("/")
def create_task(body: CreateTaskBody, request: Request) -> dict[str, Any]:
    """Create a new task."""
    if body.column not in COLUMNS:
        raise HTTPException(status_code=400, detail=f"Invalid column: {body.column}")

    store = _get_store(request)
    task = Task(
        title=body.title,
        description=body.description,
        column=body.column,
        project_id=body.project_id,
        agent=body.agent,
        priority=body.priority,
        autopilot=body.autopilot,
    )
    created = store.create(task)
    return {"task": created.to_dict(), "status": "created"}


@task_router.get("/{task_id}")
def get_task(task_id: str, request: Request) -> dict[str, Any]:
    """Get a single task."""
    store = _get_store(request)
    task = store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": task.to_dict()}


@task_router.patch("/{task_id}")
def update_task(task_id: str, body: UpdateTaskBody, request: Request) -> dict[str, Any]:
    """Update a task's fields."""
    store = _get_store(request)
    fields = body.model_dump(exclude_none=True)
    if "column" in fields and fields["column"] not in COLUMNS:
        raise HTTPException(status_code=400, detail=f"Invalid column: {fields['column']}")

    task = store.update(task_id, **fields)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": task.to_dict(), "status": "updated"}


@task_router.post("/{task_id}/move")
def move_task(task_id: str, body: MoveTaskBody, request: Request) -> dict[str, Any]:
    """Move a task to a different column."""
    store = _get_store(request)
    try:
        task = store.move(task_id, body.column)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": task.to_dict(), "status": "moved"}


@task_router.delete("/{task_id}")
def delete_task(task_id: str, request: Request) -> dict[str, str]:
    """Delete a task."""
    store = _get_store(request)
    if not store.delete(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "deleted"}


@task_router.get("/stats/summary")
def task_stats(project_id: str | None = None, request: Request = None) -> dict[str, Any]:
    """Get task board statistics."""
    store = _get_store(request)
    return store.stats(project_id)


# ── Chat history endpoints ────────────────────────────────────────────────

class ChatMessageBody(BaseModel):
    project_id: str | None = None
    agent_id: str
    role: str
    content: str


@task_router.get("/chat/{project_id}")
def get_project_chat(project_id: str, limit: int = 100, request: Request = None) -> dict[str, Any]:
    """Get chat history for a project."""
    store = _get_store(request)
    # Treat the literal string "null" or "global" as no project (global chat)
    pid = None if project_id in ("null", "global") else project_id
    messages = store.get_chat_history(pid, limit=limit)
    return {"project_id": pid, "messages": messages}


@task_router.post("/chat")
def save_chat_message(body: ChatMessageBody, request: Request) -> dict[str, str]:
    """Persist a chat message for a project."""
    store = _get_store(request)
    store.add_chat_message(
        project_id=body.project_id,
        agent_id=body.agent_id,
        role=body.role,
        content=body.content,
    )
    return {"status": "saved"}


# ── Autopilot endpoint ────────────────────────────────────────────────────

@task_router.post("/{task_id}/run")
def run_task_autopilot(
    task_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict[str, Any]:
    """Trigger the orchestrator for an autopilot task.

    Moves task: backlog/planned → in-progress → review (after run) → done (if Athena approves).
    Returns immediately; the orchestrator runs in the background.
    """
    from src.orchestrator.graph import run_task as _run_task
    from src.token_tracker.tracker import TokenTracker

    store = _get_store(request)
    task = store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.autopilot:
        raise HTTPException(status_code=400, detail="Task does not have autopilot enabled")

    tracker: TokenTracker = request.app.state.tracker
    if tracker.is_over_budget:
        raise HTTPException(status_code=429, detail="Daily call limit exhausted")

    # Move to in-progress immediately
    store.move(task_id, "in-progress")

    def _bg_run() -> None:
        try:
            result = _run_task(
                task.description or task.title,
                tracker=tracker,
                use_memory=True,
                project_id=task.project_id,
            )
            final_output = result.get("final_output", "")
            store.update(task_id, result=final_output, column="review")

            # Athena reviews the result
            agents = getattr(request.app.state, "agents", {})
            manager = agents.get("manager")
            if manager:
                review = manager.review_output(task.description or task.title, final_output)
                if review.get("verdict") == "approve":
                    store.move(task_id, "done")
                # revise/redo leaves task in "review" for human inspection
        except Exception as exc:
            logger.error("Autopilot run failed for task %s: %s", task_id, exc)
            store.update(task_id, result=f"Autopilot error: {exc}", column="review")

    background_tasks.add_task(_bg_run)
    return {"status": "running", "task_id": task_id, "message": "Autopilot started — task moved to in-progress"}
