"""Autonomous heartbeat scheduler — lets Athena act when idle.

When the user goes quiet, Athena's drives keep decaying. The heartbeat
scheduler checks periodically and, when conditions are right, picks a
task from the backlog and runs it through the orchestrator.

Driven by the Sims drive system:
- High energy + focus → pick highest-priority backlog task
- Low energy → rest (no action, drives recover)
- Low knowledge → pick a learning/research task
- Low morale → pick an easy win from the backlog
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.agents.manager import ManagerAgent
    from src.tasks.store import Task, TaskStore
    from src.token_tracker.tracker import TokenTracker

logger = logging.getLogger(__name__)

# How often the heartbeat fires (seconds)
DEFAULT_HEARTBEAT_INTERVAL = 120  # 2 minutes

# Minimum user silence before Athena acts autonomously (seconds)
MIN_IDLE_SECONDS = 300  # 5 minutes of silence

# Minimum effectiveness to pick up work (0.0–1.0)
MIN_EFFECTIVENESS = 0.35

# Maximum autonomous actions per hour (prevent runaway)
MAX_ACTIONS_PER_HOUR = 6


class HeartbeatScheduler:
    """Periodically evaluates Athena's state and picks autonomous actions.

    Lifecycle:
        scheduler = HeartbeatScheduler(manager, task_store, tracker, ...)
        await scheduler.start()
        ...
        await scheduler.stop()
    """

    def __init__(
        self,
        manager: ManagerAgent,
        task_store: TaskStore,
        tracker: TokenTracker,
        agents: dict[str, Any],
        on_action: Callable[[dict[str, Any]], Any] | None = None,
        interval: int = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self.manager = manager
        self.task_store = task_store
        self.tracker = tracker
        self.agents = agents
        self.on_action = on_action  # callback to broadcast actions (SSE/Telegram)
        self.interval = interval
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._last_user_activity: float = time.time()
        self._actions_this_hour: list[float] = []
        self._last_action_summary: dict[str, Any] = {}

    # -- public API ------------------------------------------------------------

    def record_user_activity(self) -> None:
        """Call this whenever the user sends a message or interacts."""
        self._last_user_activity = time.time()

    @property
    def idle_seconds(self) -> float:
        return time.time() - self._last_user_activity

    @property
    def is_idle(self) -> bool:
        return self.idle_seconds >= MIN_IDLE_SECONDS

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._heartbeat_loop(), name="athena-heartbeat"
        )
        logger.info(
            "Heartbeat scheduler started (interval=%ds, idle_threshold=%ds)",
            self.interval, MIN_IDLE_SECONDS,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._executor.shutdown(wait=False)
        logger.info("Heartbeat scheduler stopped")

    def status(self) -> dict[str, Any]:
        drives = self.manager.drives.state.to_dict()
        return {
            "running": self._running,
            "idle_seconds": round(self.idle_seconds, 1),
            "is_idle": self.is_idle,
            "drives": drives,
            "actions_this_hour": len(self._prune_action_timestamps()),
            "max_actions_per_hour": MAX_ACTIONS_PER_HOUR,
            "last_action": self._last_action_summary,
        }

    # -- core loop -------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Main loop: sleep → evaluate → maybe act → repeat."""
        while self._running:
            try:
                await asyncio.sleep(self.interval)
                if not self._running:
                    break

                # Only act if user is idle and we're under budget
                if not self.is_idle:
                    logger.debug("Heartbeat: user active (idle %.0fs), skipping", self.idle_seconds)
                    continue

                if self.tracker.is_over_budget:
                    logger.debug("Heartbeat: over daily budget, skipping")
                    continue

                if len(self._prune_action_timestamps()) >= MAX_ACTIONS_PER_HOUR:
                    logger.debug("Heartbeat: hourly action limit reached, resting")
                    self.manager.drives.rest()
                    continue

                # Evaluate drives and decide action
                action = await self._decide_action()
                if action:
                    await self._execute_action(action)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Heartbeat loop error")
                await asyncio.sleep(30)

    async def _decide_action(self) -> dict[str, Any] | None:
        """Inspect drives + backlog and decide what to do.

        Returns an action dict like:
            {"type": "pick_task", "task_id": "abc123", "reason": "..."}
            {"type": "rest",     "reason": "energy too low"}
            {"type": "learn",    "reason": "knowledge drive low, researching"}
            None → do nothing
        """
        drives = self.manager.drives.state
        eff = drives.overall_effectiveness()

        # If exhausted → rest
        if drives.needs_rest():
            self.manager.drives.rest()
            logger.info("Heartbeat: Athena is resting (energy=%.1f)", drives.get("energy") if isinstance(drives.get, type) else drives.levels.get("energy", 0))
            return {"type": "rest", "reason": "Energy critically low — resting"}

        # If demoralized → pick an easy win
        if drives.is_demoralized():
            task = self._find_easy_task()
            if task:
                return {
                    "type": "pick_task",
                    "task_id": task.id,
                    "task_title": task.title,
                    "reason": f"Morale low — picking easy win: {task.title}",
                }
            self.manager.drives.rest()
            return {"type": "rest", "reason": "Morale low, no easy tasks — resting"}

        # If effectiveness too low → skip
        if eff < MIN_EFFECTIVENESS:
            logger.debug("Heartbeat: effectiveness %.2f below threshold, skipping", eff)
            return None

        # Low knowledge → look for learning tasks
        from src.agents.sims.drives import DriveType
        if drives.levels.get(DriveType.KNOWLEDGE, 50) < 30:
            task = self._find_task_by_tag("research") or self._find_task_by_tag("learning")
            if task:
                return {
                    "type": "pick_task",
                    "task_id": task.id,
                    "task_title": task.title,
                    "reason": f"Knowledge low — learning: {task.title}",
                }

        # Normal: pick highest priority backlog task
        task = self._find_best_task()
        if task:
            return {
                "type": "pick_task",
                "task_id": task.id,
                "task_title": task.title,
                "reason": f"Picking top backlog task: {task.title}",
            }

        # Nothing to do
        logger.debug("Heartbeat: no actionable tasks in backlog")
        return None

    async def _execute_action(self, action: dict[str, Any]) -> None:
        """Execute the decided action."""
        action_type = action["type"]
        self._actions_this_hour.append(time.time())
        self._last_action_summary = {**action, "timestamp": time.time()}

        if action_type == "rest":
            logger.info("Heartbeat action: REST — %s", action["reason"])
            self._broadcast(action)
            return

        if action_type == "pick_task":
            task_id = action["task_id"]
            logger.info("Heartbeat action: PICK TASK %s — %s", task_id, action["reason"])

            # Move task to in-progress
            self.task_store.move(task_id, "in-progress")
            task = self.task_store.get(task_id)
            if not task:
                return

            # Run through orchestrator in thread pool
            loop = asyncio.get_event_loop()
            try:
                result = await loop.run_in_executor(
                    self._executor,
                    self._run_task_sync,
                    task,
                )
                # Move to review and store result
                self.task_store.update(task_id, result=result, column="review")
                self.manager.drives.record_success()

                action["result"] = result[:500]  # truncate for broadcast
                action["status"] = "completed"
                logger.info("Heartbeat: task %s completed, moved to review", task_id)

            except Exception as exc:
                logger.exception("Heartbeat: task %s failed", task_id)
                self.task_store.update(task_id, result=f"Error: {exc}", column="backlog")
                self.manager.drives.record_failure()
                action["status"] = "failed"
                action["error"] = str(exc)

            self._broadcast(action)

    def _run_task_sync(self, task: Task) -> str:
        """Execute a task via the orchestrator (blocking)."""
        from src.orchestrator.graph import run_task
        result = run_task(
            task=f"{task.title}\n\n{task.description}",
            tracker=self.tracker,
            use_memory=bool(self.manager.memory),
            project_id=task.project_id,
        )
        return result.get("final_output", "No output produced")

    # -- task selection --------------------------------------------------------

    def _find_best_task(self) -> Task | None:
        """Find the highest-priority backlog task."""
        tasks = self.task_store.list_by_column("backlog")
        # Filter to autopilot-enabled tasks only for autonomous execution
        autopilot_tasks = [t for t in tasks if t.autopilot]
        if not autopilot_tasks:
            return None
        # Sort by priority desc, then oldest first
        autopilot_tasks.sort(key=lambda t: (-t.priority, t.created_at))
        return autopilot_tasks[0]

    def _find_easy_task(self) -> Task | None:
        """Find a low-priority backlog task (easy win for morale)."""
        tasks = self.task_store.list_by_column("backlog")
        autopilot_tasks = [t for t in tasks if t.autopilot]
        if not autopilot_tasks:
            return None
        # Sort by priority ASC (easiest first)
        autopilot_tasks.sort(key=lambda t: (t.priority, t.created_at))
        return autopilot_tasks[0]

    def _find_task_by_tag(self, tag: str) -> Task | None:
        """Find a backlog task with a matching tag in metadata."""
        tasks = self.task_store.list_by_column("backlog")
        for t in tasks:
            if t.autopilot and tag in (t.metadata.get("tags") or []):
                return t
        return None

    # -- helpers ---------------------------------------------------------------

    def _prune_action_timestamps(self) -> list[float]:
        """Remove actions older than 1 hour and return current list."""
        cutoff = time.time() - 3600
        self._actions_this_hour = [ts for ts in self._actions_this_hour if ts > cutoff]
        return self._actions_this_hour

    def _broadcast(self, action: dict[str, Any]) -> None:
        """Send action to callback (Telegram/SSE)."""
        if self.on_action:
            try:
                self.on_action(action)
            except Exception:
                logger.exception("Heartbeat broadcast error")
