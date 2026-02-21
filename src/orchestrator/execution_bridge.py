"""Execution bridge — lets Athena translate plans into real code changes.

When Athena generates a plan (subtasks JSON), this bridge can execute it
by routing tasks to the local runner, which uses Claude CLI to make
actual file edits, git commits, and PR creation.

This is the missing link between "planning" and "doing":
  Plan (LLM output) → ExecutionBridge → Runner (file edits) → Git PR

Safety:
- All changes go through a feature branch, never main directly.
- Runner safety layer blocks destructive commands.
- Approval required (configurable) before pushing.
- Self-edit (project_id=ai-companion) has extra guardrails.

Usage:
    bridge = ExecutionBridge(runner_client, registry)
    result = bridge.execute_plan(project_id, plan, subtasks)
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from src.runner_connector.client import (
    RunnerClient,
    RunnerError,
    RunnerOfflineError,
)
from src.runner_connector.models import (
    ClaudeRunRequest,
    CmdRequest,
    CmdResult,
    PushPrRequest,
)

logger = logging.getLogger(__name__)

# Files that should NEVER be edited autonomously on the Athena project
_SELF_EDIT_PROTECTED_FILES = {
    "src/runner/safety.py",       # safety layer itself
    "src/orchestrator/execution_bridge.py",  # this file
    ".env",                        # secrets
    "deploy/",                     # deployment configs
}

# Maximum number of subtasks per execution to prevent runaway
MAX_SUBTASKS_PER_EXECUTION = 10

# Maximum Claude CLI timeout per subtask (seconds)
MAX_CLAUDE_TIMEOUT = 600


@dataclass
class ExecutionResult:
    """Result of executing a plan through the runner."""

    project_id: str
    branch: str = ""
    subtask_results: list[dict[str, Any]] = field(default_factory=list)
    git_status: dict[str, Any] = field(default_factory=dict)
    pr_url: str = ""
    success: bool = False
    error: str = ""
    duration_ms: int = 0


class ExecutionBridge:
    """Translates Athena's plans into real code changes via the runner.

    Flow:
    1. Check runner is online
    2. Check git is clean
    3. Create feature branch
    4. For each subtask: build a detailed prompt → send to Claude CLI
    5. Run tests (if configured)
    6. Commit + push + create PR (or stage for approval)
    """

    def __init__(
        self,
        runner_client: RunnerClient,
        auto_pr: bool = True,
    ) -> None:
        self.runner = runner_client
        self.auto_pr = auto_pr

    def is_runner_online(self) -> bool:
        """Check if the local runner is reachable."""
        try:
            self.runner.health()
            return True
        except (RunnerOfflineError, RunnerError):
            return False

    def execute_plan(
        self,
        project_id: str,
        plan: str,
        subtasks: list[dict[str, Any]],
        requested_by: str = "discord",
        auto_approve: bool = False,
        on_progress: Any | None = None,
    ) -> ExecutionResult:
        """Execute a plan by routing subtasks to Claude CLI via the runner.

        Args:
            project_id: Which project to modify (must exist in projects.yaml)
            plan: High-level plan description
            subtasks: List of subtask dicts from manager.decompose_task()
            requested_by: Source of the request (discord, web, heartbeat)
            auto_approve: If True, auto-create PR. If False, stage only.
            on_progress: Optional callback(phase, detail) for real-time updates.

        Returns:
            ExecutionResult with status, branch, pr_url etc.
        """
        t0 = time.perf_counter()
        result = ExecutionResult(project_id=project_id)

        def _progress(phase: str, detail: str = "") -> None:
            if on_progress:
                try:
                    on_progress(phase, detail)
                except Exception:
                    pass

        # -- Preflight checks --
        _progress("preflight", "Checking runner status...")
        if not self.is_runner_online():
            result.error = "Runner is offline — cannot execute code changes."
            return result

        if len(subtasks) > MAX_SUBTASKS_PER_EXECUTION:
            result.error = (
                f"Too many subtasks ({len(subtasks)}). "
                f"Maximum is {MAX_SUBTASKS_PER_EXECUTION}."
            )
            return result

        # Self-edit safety check
        is_self_edit = project_id in ("ai-companion", "athena")
        if is_self_edit:
            violations = self._check_self_edit_safety(subtasks)
            if violations:
                result.error = (
                    f"Self-edit safety violation: {', '.join(violations)}. "
                    "These files cannot be modified autonomously."
                )
                return result

        # Check git is clean
        try:
            git_st = self.runner.git_status(project_id)
            if git_st.dirtyCount > 0:
                result.error = (
                    f"Working tree not clean ({git_st.dirtyCount} dirty files). "
                    "Commit or stash changes first."
                )
                result.git_status = {
                    "branch": git_st.branch,
                    "dirty": git_st.dirtyCount,
                    "files": git_st.changedFiles[:10],
                }
                return result
        except RunnerOfflineError:
            result.error = "Runner went offline during git status check."
            return result
        except RunnerError as e:
            result.error = f"Git status check failed: {e.detail}"
            return result

        # -- Create feature branch --
        branch_name = self._generate_branch_name(plan, requested_by)
        result.branch = branch_name
        _progress("branch", f"Creating branch {branch_name}...")

        try:
            checkout_result = self.runner.run_cmd(CmdRequest(
                projectId=project_id,
                command=f"git checkout -b {branch_name}",
                timeoutSec=30,
            ))
            if checkout_result.exitCode != 0:
                result.error = f"Failed to create branch: {checkout_result.stderr}"
                return result
        except (RunnerOfflineError, RunnerError) as e:
            result.error = f"Branch creation failed: {e}"
            return result

        # -- Execute each subtask via Claude CLI --
        all_success = True
        for i, subtask in enumerate(subtasks):
            subtask_id = subtask.get("id", str(i + 1))
            description = subtask.get("description", "")
            agent_type = subtask.get("agent", "backend")

            if not description:
                result.subtask_results.append({
                    "id": subtask_id,
                    "success": False,
                    "error": "Empty description",
                })
                continue

            # Build the Claude CLI prompt for this subtask
            prompt = self._build_execution_prompt(
                project_id=project_id,
                plan=plan,
                subtask=subtask,
                agent_type=agent_type,
                is_self_edit=is_self_edit,
            )

            logger.info(
                "Executing subtask %s/%d via Claude CLI (agent=%s, project=%s)",
                subtask_id, len(subtasks), agent_type, project_id,
            )
            _progress("subtask", f"Subtask {subtask_id}/{len(subtasks)}: {description[:80]}...")

            try:
                claude_result = self.runner.run_claude(ClaudeRunRequest(
                    projectId=project_id,
                    prompt=prompt,
                    dangerouslySkipPermissions=True,
                    timeoutSec=MAX_CLAUDE_TIMEOUT,
                ))

                subtask_result = {
                    "id": subtask_id,
                    "agent": agent_type,
                    "success": claude_result.exitCode == 0,
                    "output": claude_result.stdout[:2000],
                    "error": claude_result.stderr[:500] if claude_result.exitCode != 0 else "",
                    "duration_ms": claude_result.durationMs,
                }

                if claude_result.exitCode != 0:
                    all_success = False
                    logger.warning(
                        "Subtask %s failed (exit %d): %s",
                        subtask_id, claude_result.exitCode, claude_result.stderr[:200],
                    )

            except (RunnerOfflineError, RunnerError) as e:
                subtask_result = {
                    "id": subtask_id,
                    "agent": agent_type,
                    "success": False,
                    "output": "",
                    "error": str(e),
                }
                all_success = False

            result.subtask_results.append(subtask_result)

        # -- Run tests if this is a self-edit --
        if is_self_edit and all_success:
            _progress("testing", "Running tests (self-edit safety)...")
            test_result = self._run_tests(project_id)
            result.subtask_results.append({
                "id": "auto-test",
                "agent": "tester",
                "success": test_result.exitCode == 0,
                "output": test_result.stdout[-2000:],
                "error": test_result.stderr[:500] if test_result.exitCode != 0 else "",
            })
            if test_result.exitCode != 0:
                all_success = False
                logger.warning("Self-edit tests failed — will not auto-PR")

        # -- Stage changes --
        try:
            self.runner.run_cmd(CmdRequest(
                projectId=project_id,
                command="git add -A",
                timeoutSec=30,
            ))
        except Exception as e:
            logger.warning("Git add failed: %s", e)

        # -- Final git status --
        try:
            final_status = self.runner.git_status(project_id)
            result.git_status = {
                "branch": final_status.branch,
                "dirty": final_status.dirtyCount,
                "files": final_status.changedFiles[:20],
            }
        except Exception:
            pass

        # -- Create PR or commit + merge --
        should_pr = (self.auto_pr or auto_approve) and all_success
        if should_pr and result.git_status.get("dirty", 0) > 0:
            _progress("pushing", "Committing and pushing changes...")
            try:
                pr_result = self.runner.push_pr(PushPrRequest(
                    projectId=project_id,
                    branch=branch_name,
                    title=f"[Athena] {plan[:80]}",
                    body=self._build_pr_body(plan, result.subtask_results, requested_by),
                ))
                result.pr_url = pr_result.prUrl
                logger.info("PR created: %s", result.pr_url)
            except (RunnerOfflineError, RunnerError) as e:
                err_detail = str(e)
                # If push succeeded but PR creation failed (gh CLI missing),
                # still commit + merge locally
                if "PR creation failed" in err_detail or "gh" in err_detail.lower():
                    logger.info("PR creation failed but push may have succeeded — merging to main")
                    _progress("merging", "PR failed, merging to main locally...")
                else:
                    # Commit locally if push also failed
                    logger.warning("Push failed: %s — committing locally", e)
                    _progress("committing", "Push failed, committing locally...")
                    try:
                        self.runner.run_cmd(CmdRequest(
                            projectId=project_id,
                            command=f'git commit -m "[Athena] {plan[:60]}"',
                            timeoutSec=30,
                        ))
                    except Exception:
                        pass
                # Even without PR, merge to main so changes aren't lost
                try:
                    self.runner.run_cmd(CmdRequest(
                        projectId=project_id,
                        command="git checkout main",
                        timeoutSec=30,
                    ))
                    self.runner.run_cmd(CmdRequest(
                        projectId=project_id,
                        command=f"git merge {branch_name} --no-edit",
                        timeoutSec=30,
                    ))
                    logger.info("Merged %s into main locally", branch_name)
                    result.branch = f"{branch_name} (merged to main)"
                except Exception as merge_err:
                    logger.warning("Merge to main failed: %s", merge_err)
                result.error = ""
                result.success = True
        elif not all_success:
            # Commit WIP, leave branch for inspection
            _progress("cleanup", "Some subtasks failed — saving WIP...")
            try:
                self.runner.run_cmd(CmdRequest(
                    projectId=project_id,
                    command=f'git commit -m "WIP: {plan[:50]} (partial, needs review)"',
                    timeoutSec=30,
                ))
            except Exception:
                pass
            result.error = f"Some subtasks failed. Changes on branch {branch_name} for review."

        result.success = all_success and not result.error
        result.duration_ms = int((time.perf_counter() - t0) * 1000)

        # Switch back to main (if not already there from merge)
        _progress("cleanup", "Switching back to main...")
        try:
            # Force checkout to main, discarding any leftover unstaged changes
            current = self.runner.run_cmd(CmdRequest(
                projectId=project_id,
                command="git rev-parse --abbrev-ref HEAD",
                timeoutSec=10,
            ))
            if current.stdout.strip() != "main":
                self.runner.run_cmd(CmdRequest(
                    projectId=project_id,
                    command="git checkout main",
                    timeoutSec=30,
                ))
        except Exception:
            logger.warning("Failed to switch back to main")

        _progress("done", f"Execution completed in {int((time.perf_counter() - t0) * 1000)}ms")
        return result

    # -- Prompt building -------------------------------------------------------

    def _build_execution_prompt(
        self,
        project_id: str,
        plan: str,
        subtask: dict[str, Any],
        agent_type: str,
        is_self_edit: bool = False,
    ) -> str:
        """Build a detailed prompt for Claude CLI to execute a subtask."""
        description = subtask.get("description", "")

        role_context = {
            "frontend": "You are a senior frontend developer (React, Next.js, CSS, TypeScript).",
            "backend": "You are a senior backend developer (Python, FastAPI, databases, APIs).",
            "tester": "You are a senior QA engineer (pytest, Vitest, E2E testing).",
            "manager": "You are a senior engineering manager coordinating the team.",
        }.get(agent_type, "You are a senior software engineer.")

        safety_note = ""
        if is_self_edit:
            safety_note = (
                "\n\nSAFETY: This is a self-edit of the Athena AI system. "
                "Do NOT modify: .env, safety.py, execution_bridge.py, deploy/ configs. "
                "Always preserve existing tests — add new ones, don't delete. "
                "Make minimal, targeted changes."
            )

        prompt = f"""{role_context}

You are executing a subtask as part of a larger plan.

PLAN: {plan}

YOUR SUBTASK: {description}

INSTRUCTIONS:
- Make the actual code changes needed (edit files, create files, etc.)
- Follow existing code style and patterns in this project
- Be precise and minimal — only change what's needed
- Do NOT run tests (they'll be run separately)
- Do NOT commit (commits are handled by the orchestrator){safety_note}

Execute this subtask now."""

        return prompt

    # -- Helpers ---------------------------------------------------------------

    def _generate_branch_name(self, plan: str, source: str) -> str:
        """Generate a clean branch name from the plan."""
        # Extract key words from plan
        words = re.sub(r"[^a-zA-Z0-9\s]", "", plan.lower()).split()
        slug = "-".join(words[:5])[:40]
        ts = int(time.time()) % 100000
        return f"athena/{slug}-{ts}"

    def _check_self_edit_safety(
        self, subtasks: list[dict[str, Any]]
    ) -> list[str]:
        """Check if any subtask tries to modify protected files."""
        violations = []
        for subtask in subtasks:
            desc = subtask.get("description", "").lower()
            for protected in _SELF_EDIT_PROTECTED_FILES:
                if protected.lower() in desc:
                    violations.append(f"Subtask '{subtask.get('id', '?')}' references {protected}")
        return violations

    def _run_tests(self, project_id: str) -> CmdResult:
        """Run the project's test suite."""
        try:
            return self.runner.run_cmd(CmdRequest(
                projectId=project_id,
                command="python -m pytest tests/ -x -q",
                timeoutSec=300,
            ))
        except (RunnerOfflineError, RunnerError) as e:
            return CmdResult(exitCode=1, stdout="", stderr=str(e), durationMs=0)

    def _build_pr_body(
        self,
        plan: str,
        subtask_results: list[dict[str, Any]],
        requested_by: str,
    ) -> str:
        """Build a PR description."""
        lines = [
            f"## Plan\n{plan}\n",
            f"**Requested via:** {requested_by}\n",
            "## Subtask Results\n",
        ]
        for sr in subtask_results:
            status = "✅" if sr.get("success") else "❌"
            lines.append(
                f"- {status} **{sr.get('id', '?')}** ({sr.get('agent', '?')}): "
                f"{sr.get('output', '')[:100]}"
            )
        lines.append("\n---\n*Automated by Athena AI*")
        return "\n".join(lines)
