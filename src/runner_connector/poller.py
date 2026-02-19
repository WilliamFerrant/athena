"""Background poller — periodically checks runner health and stores state."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from src.runner_connector.client import RunnerClient, RunnerOfflineError

logger = logging.getLogger(__name__)


class RunnerState:
    """Thread-safe state container for runner online/offline status."""

    def __init__(self) -> None:
        self.online: bool = False
        self.last_seen: str | None = None
        self.last_check: str | None = None
        self.version: str | None = None
        self.platform: str | None = None
        self.error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "last_seen": self.last_seen,
            "last_check": self.last_check,
            "version": self.version,
            "platform": self.platform,
            "error": self.error,
        }


class RunnerPoller:
    """Polls runner /health every N seconds in the background."""

    def __init__(
        self,
        client: RunnerClient,
        interval: float = 10.0,
    ) -> None:
        self.client = client
        self.interval = interval
        self.state = RunnerState()
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Runner poller started (interval=%ss)", self.interval)

    async def stop(self) -> None:
        """Stop the background poller."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Runner poller stopped")

    async def _poll_loop(self) -> None:
        """Polling loop — runs until stopped."""
        while self._running:
            await self._check_once()
            await asyncio.sleep(self.interval)

    async def _check_once(self) -> None:
        """Single health check against the runner."""
        now = datetime.now(timezone.utc).isoformat()
        self.state.last_check = now

        try:
            # Run sync httpx call in executor to avoid blocking event loop
            loop = asyncio.get_event_loop()
            health = await loop.run_in_executor(None, self.client.health)
            self.state.online = True
            self.state.last_seen = now
            self.state.version = health.version
            self.state.platform = health.platform
            self.state.error = None
            if not self.state.online:
                logger.info("Runner came online: %s v%s", health.platform, health.version)
        except RunnerOfflineError:
            if self.state.online:
                logger.warning("Runner went offline")
            self.state.online = False
            self.state.error = "Runner unreachable"
        except Exception as e:
            if self.state.online:
                logger.warning("Runner check failed: %s", e)
            self.state.online = False
            self.state.error = str(e)
