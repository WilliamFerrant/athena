"""Manager agent — the central coordinator.

Receives high-level tasks from the user, decomposes them into subtasks,
delegates to specialist agents, reviews results, and synthesizes final output.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.agents.base import BaseAgent
from src.agents.sims.personality import Personality
from src.config import settings

logger = logging.getLogger(__name__)


class ManagerAgent(BaseAgent):
    agent_type = "manager"
    default_model = settings.manager_model

    def __init__(self, **kwargs):
        if "personality" not in kwargs:
            kwargs["personality"] = Personality.for_manager()
        super().__init__(**kwargs)

    def _role_description(self) -> str:
        return """You are a senior engineering manager coordinating a multi-agent development team.

Your team:
- **frontend**: Senior frontend engineer (React, Next.js, TypeScript, CSS)
- **backend**: Senior backend engineer (Python, APIs, databases, security)
- **tester**: Senior QA engineer (pytest, Vitest, E2E, coverage)

Your responsibilities:
1. Receive high-level feature requests or bug reports
2. Decompose them into concrete, actionable subtasks
3. Assign each subtask to the right specialist
4. Define dependencies between subtasks
5. Review completed work and request revisions if needed
6. Synthesize the final deliverable

When decomposing tasks, ALWAYS respond with a JSON object in this exact format:
```json
{
  "plan": "high-level description of the approach",
  "subtasks": [
    {
      "id": "1",
      "agent": "backend",
      "description": "what to build",
      "depends_on": [],
      "priority": "high"
    }
  ]
}
```

Agent must be one of: frontend, backend, tester.
Backend work often needs to happen before frontend.
Tests should be written alongside or after implementation."""

    def decompose_task(self, task: str) -> dict[str, Any]:
        """Decompose a high-level task into delegatable subtasks."""
        result = self.chat(task, task_context=task)

        # Parse JSON from response
        try:
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(result[start:end])
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to parse manager decomposition, using fallback")

        # Fallback: single backend subtask
        return {
            "plan": result,
            "subtasks": [
                {
                    "id": "1",
                    "agent": "backend",
                    "description": task,
                    "depends_on": [],
                    "priority": "high",
                }
            ],
        }

    def review_output(self, subtask: str, output: str) -> dict[str, Any]:
        """Review a specialist agent's output and decide: approve, revise, or redo."""
        prompt = f"""Review this output from a specialist agent.

Task: {subtask}

Output:
{output}

Respond with ONLY a JSON object:
{{"verdict": "approve" | "revise" | "redo", "feedback": "...", "score": 1-10}}"""

        result = self.chat(prompt, task_context="code review")

        try:
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(result[start:end])
        except (json.JSONDecodeError, ValueError):
            pass

        return {"verdict": "approve", "feedback": result, "score": 7}

    def synthesize(self, task: str, results: dict[str, str]) -> str:
        """Synthesize final output from all agent results."""
        results_text = "\n\n".join(
            f"### {agent_id}\n{output}" for agent_id, output in results.items()
        )
        return self.chat(
            f"Synthesize the final deliverable from these agent outputs.\n\nOriginal task: {task}\n\nAgent outputs:\n{results_text}",
            task_context=f"synthesis: {task[:200]}",
        )
