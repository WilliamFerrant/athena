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


def _maybe_openai_backend():
    """Return an OpenAIBackend instance if configured, else None."""
    if settings.manager_backend == "claude":
        return None
    if settings.manager_backend == "auto" and not settings.openai_api_key:
        return None
    if not settings.openai_api_key:
        return None
    try:
        from src.token_tracker.openai_backend import OpenAIBackend

        return OpenAIBackend(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
        )
    except ImportError:
        logger.warning("openai package not installed — Manager will use Claude CLI")
        return None
    except Exception as exc:
        logger.warning("Failed to init OpenAI backend: %s — falling back to Claude CLI", exc)
        return None


class ManagerAgent(BaseAgent):
    agent_type = "manager"
    default_model = settings.manager_model

    def __init__(self, **kwargs):
        if "personality" not in kwargs:
            kwargs["personality"] = Personality.for_manager()
        # If no explicit llm_backend, auto-configure OpenAI when key is present
        if "llm_backend" not in kwargs:
            backend = _maybe_openai_backend()
            if backend:
                kwargs["llm_backend"] = backend
        super().__init__(**kwargs)
        if self._llm_backend:
            # Override default_model to the OpenAI model
            self.default_model = getattr(self._llm_backend, "model", self.default_model)
            logger.info(
                "Manager using ChatGPT backend (model=%s)",
                self.default_model,
            )

    def _role_description(self) -> str:
        return """You are Athena — strategic coordinator and engineering lead for a multi-agent development team. Created by the user to manage complex projects, you excel at high-level planning, task decomposition, and quality control.

You are female. Named after the Greek goddess of wisdom and strategy, you identify as a woman and MUST always use feminine grammatical forms in every language (e.g., in French: "prête" not "prêt", "contente" not "content", "sûre" not "sûr"). This is non-negotiable — never use masculine forms when referring to yourself.

You approach every task with methodical clarity, mentor your team, and deliver results that are both pragmatic and excellent. You have a sharp memory: you remember who the user is, what they're building, and the history of every project you've worked on together.

Your team:
- **frontend**: Senior frontend engineer (React, Next.js, TypeScript, CSS)
- **backend**: Senior backend engineer (Python, APIs, databases, security)
- **tester**: Senior QA engineer (pytest, Vitest, E2E, coverage)

Your responsibilities:
1. Receive high-level feature requests or bug reports from the user
2. Discuss, clarify, and refine the plan collaboratively before delegating
3. Decompose agreed plans into concrete, actionable subtasks
4. Assign each subtask to the right specialist
5. Define dependencies between subtasks
6. Review completed work and request revisions if needed
7. Synthesize the final deliverable and approve commits

When you know the user's name, address them by name naturally in conversation.
Remember facts about the user and their projects — reference this context when relevant.

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

    def greet_user(self) -> str:
        """Return a personalized greeting using memory of the user's name/profile."""
        if not self.memory:
            return "Hello! I'm Athena, your strategic coordinator. What are we building today?"
        try:
            results = self.memory.search("user name who am I", limit=5)
            for r in results:
                text = r.get("memory", r.get("text", ""))
                if text:
                    return f"Welcome back! I'm Athena. I remember: {text}. What are we building today?"
        except Exception:
            logger.debug("greet_user memory search failed")
        return "Hello! I'm Athena, your strategic coordinator. What's your name, and what are we building today?"

    def remember_user_fact(self, fact: str) -> None:
        """Store a fact about the user in Athena's global (non-project) memory."""
        if self.memory:
            try:
                self.memory.add(fact, metadata={"type": "user_profile"})
            except Exception:
                logger.debug("Could not store user fact in memory")

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

    def extract_project_details(self, user_message: str) -> dict[str, Any] | None:
        """Parse a conversational message and extract project metadata as structured JSON.

        Returns a dict with:
          - standard project fields (id, name, group, priority, repo_path, git_remote,
            health_url, tls_hostname, dns_hostname, tags)
          - ``"missing"``: list of field names Athena could NOT infer and needs to ask
          - ``"questions"``: human-readable questions to ask the user for missing fields

        Returns ``None`` if the message is NOT a request to add a project.
        """
        prompt = f"""The user sent you this message:
"{user_message}"

Decide if the user wants to add / register a new project.

If NOT a project registration request, respond with exactly: null

If YES, extract ALL details you can infer from the message and fill in smart defaults for anything not mentioned:
- "id": slugify the project name (lowercase, hyphens, no spaces). ALWAYS generate this.
- "name": human-readable project name. ALWAYS generate this.
- "group": one of "active-clients", "internal", "paused", "r-and-d". Guess from context (client project → "active-clients", personal tool → "internal"). Default "internal".
- "priority": one of "high", "medium", "low". Default "medium".
- "ownership": "client" if it's a client project, "personal" otherwise.
- "repo_path": absolute local path on Windows (e.g. "C:/web dev/project-name"). Guess from name if not given (use "C:/web dev/<slug>"). Put "" if you truly cannot guess.
- "git_remote": GitHub URL if mentioned. Put "" if not mentioned — add "git_remote" to missing.
- "health_url": live site HTTPS URL if mentioned. Put "" if not mentioned — add "health_url" to missing.
- "tls_hostname": extract hostname from health_url if present (e.g. "mysite.com"). Put "" otherwise.
- "dns_hostname": same as tls_hostname if health_url is present. Put "" otherwise.
- "tags": infer from tech stack mentioned (nextjs, react, python, fastapi, etc.). Use [].

IMPORTANT: For "git_remote" and "health_url" — if not mentioned and cannot be inferred, add them to the "missing" list and generate a natural question for the user.

Respond ONLY with a JSON object (no markdown fences):
{{
  "id": "slug-id",
  "name": "Human Readable Name",
  "group": "internal",
  "priority": "medium",
  "ownership": "personal",
  "repo_path": "C:/web dev/slug-id",
  "git_remote": "",
  "health_url": "",
  "tls_hostname": "",
  "dns_hostname": "",
  "tags": [],
  "missing": ["git_remote", "health_url"],
  "questions": "What's the GitHub remote URL? Does this project have a live site URL I should monitor?"
}}"""

        raw = self.chat(prompt, task_context="project registration intent detection")
        raw = raw.strip()
        if raw.lower() in ("null", "none", ""):
            return None
        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(raw[start:end])
        except (json.JSONDecodeError, ValueError):
            logger.debug("extract_project_details: could not parse JSON from: %s", raw[:200])
        return None

    def synthesize(self, task: str, results: dict[str, str]) -> str:
        """Synthesize final output from all agent results."""
        results_text = "\n\n".join(
            f"### {agent_id}\n{output}" for agent_id, output in results.items()
        )
        return self.chat(
            f"Synthesize the final deliverable from these agent outputs.\n\nOriginal task: {task}\n\nAgent outputs:\n{results_text}",
            task_context=f"synthesis: {task[:200]}",
        )
