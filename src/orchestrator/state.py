"""LangGraph state definitions for the multi-agent workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langgraph.graph import MessagesState


@dataclass
class SubtaskResult:
    subtask_id: str
    agent_type: str
    description: str
    output: str = ""
    review_verdict: Literal["approve", "revise", "redo", "pending"] = "pending"
    review_feedback: str = ""
    review_score: int = 0
    attempts: int = 0


class WorkflowState(MessagesState):
    """Full state flowing through the LangGraph orchestration graph.

    Extends MessagesState (which provides `messages: list`) with our
    custom fields for multi-agent coordination.

    Note: This is a TypedDict — instances are plain dicts at runtime.
    """

    # The original high-level task from the user
    task: str
    # Manager's decomposition plan
    plan: str
    subtasks: list[dict[str, Any]]
    # Results keyed by subtask ID
    results: dict[str, SubtaskResult]
    # Current phase
    phase: Literal[
        "intake",
        "planning",
        "executing",
        "reviewing",
        "synthesizing",
        "done",
    ]
    # The next subtask IDs ready for execution (dependencies resolved)
    ready_queue: list[str]
    # Final synthesized output
    final_output: str
    # Error tracking
    errors: list[str]
    # Iteration guard
    max_revisions: int
