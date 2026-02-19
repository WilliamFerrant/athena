"""Health check scheduler — runs checks at configured intervals.

Uses APScheduler to schedule periodic checks from the ProjectRegistry.
Results are stored in HealthStore and broadcast via SSE callbacks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..projects.registry import ProjectRegistry
from .engine import CheckResult, HealthStore, execute_check

logger = logging.getLogger(__name__)


class HealthScheduler:
    """Schedules and executes health checks for all registered projects.

    Uses a simple asyncio loop instead of APScheduler to minimize deps.
    Each check runs in a thread pool to avoid blocking the event loop.
    """

    def __init__(
        self,
        registry: ProjectRegistry,
        store: HealthStore,
        on_result: Callable[[CheckResult], Any] | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.on_result = on_result  # SSE broadcast callback
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    async def start(self) -> None:
        """Start scheduling all health checks."""
        if self._running:
            return
        self._running = True

        checks = self.registry.all_health_checks()
        if not checks:
            logger.info("No health checks configured — scheduler idle")
            return

        for project, check_def in checks:
            interval = check_def.interval_seconds
            task = asyncio.create_task(
                self._check_loop(project.id, check_def, interval),
                name=f"health-{project.id}-{check_def.id}",
            )
            self._tasks.append(task)

        logger.info(
            "Health scheduler started: %d checks across %d projects",
            len(checks),
            len({p.id for p, _ in checks}),
        )

    async def stop(self) -> None:
        """Stop all check loops."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._executor.shutdown(wait=False)
        logger.info("Health scheduler stopped")

    async def run_all_now(self) -> list[CheckResult]:
        """Run all checks immediately (for manual trigger / startup)."""
        checks = self.registry.all_health_checks()
        results = []
        loop = asyncio.get_event_loop()

        for project, check_def in checks:
            result = await loop.run_in_executor(
                self._executor, execute_check, check_def, project.id,
            )
            self.store.store_result(result)
            results.append(result)
            if self.on_result:
                try:
                    self.on_result(result)
                except Exception:
                    logger.exception("SSE callback error")

        return results

    async def run_project_checks(self, project_id: str) -> list[CheckResult]:
        """Run all checks for a specific project."""
        project = self.registry.get(project_id)
        if not project or not project.health_checks:
            return []

        results = []
        loop = asyncio.get_event_loop()

        for check_def in project.health_checks:
            result = await loop.run_in_executor(
                self._executor, execute_check, check_def, project.id,
            )
            self.store.store_result(result)
            results.append(result)
            if self.on_result:
                try:
                    self.on_result(result)
                except Exception:
                    logger.exception("SSE callback error")

        return results

    async def _check_loop(self, project_id: str, check_def: Any, interval: int) -> None:
        """Persistent loop that runs a single check at its interval."""
        loop = asyncio.get_event_loop()

        # Run immediately on start
        try:
            result = await loop.run_in_executor(
                self._executor, execute_check, check_def, project_id,
            )
            self.store.store_result(result)
            if self.on_result:
                try:
                    self.on_result(result)
                except Exception:
                    logger.exception("SSE callback error")
        except Exception:
            logger.exception("Health check error: %s/%s", project_id, check_def.id)

        # Then repeat at interval
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    break

                result = await loop.run_in_executor(
                    self._executor, execute_check, check_def, project_id,
                )
                self.store.store_result(result)

                if self.on_result:
                    try:
                        self.on_result(result)
                    except Exception:
                        logger.exception("SSE callback error")

                logger.debug(
                    "Check %s/%s: %s (%dms)",
                    project_id, check_def.id, result.status.value, result.latency_ms,
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Health check error: %s/%s", project_id, check_def.id)
                await asyncio.sleep(min(interval, 60))
