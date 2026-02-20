"""Background poller — periodically checks runner health and stores state.

Features:
- Exponential backoff on consecutive failures (10s → 20s → 40s … 300s)
- Instant recovery: resets to base interval on first success after failure
- Tracks consecutive failure count + reconnect attempts for diagnostics
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from src.runner_connector.client import RunnerClient, RunnerOfflineError

logger = logging.getLogger(__name__)

# Backoff constants
_BASE_INTERVAL = 10.0    # seconds
_MAX_INTERVAL = 300.0    # 5 minutes cap
_BACKOFF_FACTOR = 2.0


class RunnerState:
    """Thread-safe state container for runner online/offline status."""

    def __init__(self) -> None:
        self.online: bool = False
        self.last_seen: str | None = None
        self.last_check: str | None = None
        self.version: str | None = None
        self.platform: str | None = None
        self.error: str | None = None
        # Diagnostics
        self.consecutive_failures: int = 0
        self.reconnect_attempts: int = 0
        self.current_interval: float = _BASE_INTERVAL
        self.last_transition: str | None = None  # "online→offline" timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "last_seen": self.last_seen,
            "last_check": self.last_check,
            "version": self.version,
            "platform": self.platform,
            "error": self.error,
            "consecutive_failures": self.consecutive_failures,
            "reconnect_attempts": self.reconnect_attempts,
            "current_interval": round(self.current_interval, 1),
            "last_transition": self.last_transition,
        }


class RunnerPoller:
    """Polls runner /health with exponential backoff on failure."""

    def __init__(
        self,
        client: RunnerClient,
        interval: float = _BASE_INTERVAL,
        max_interval: float = _MAX_INTERVAL,
    ) -> None:
        self.client = client
        self.base_interval = interval
        self.max_interval = max_interval
        self.state = RunnerState()
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Runner poller started (interval=%ss, max=%ss)", self.base_interval, self.max_interval)

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
        """Polling loop with adaptive interval."""
        while self._running:
            await self._check_once()
            await asyncio.sleep(self.state.current_interval)

    async def _check_once(self) -> None:
        """Single health check with backoff logic."""
        now = datetime.now(timezone.utc).isoformat()
        self.state.last_check = now
        was_online = self.state.online

        try:
            loop = asyncio.get_event_loop()
            health = await loop.run_in_executor(None, self.client.health)

            # ── Success ──
            self.state.online = True
            self.state.last_seen = now
            self.state.version = health.version
            self.state.platform = health.platform
            self.state.error = None
            self.state.consecutive_failures = 0
            self.state.current_interval = self.base_interval  # reset to fast

            if not was_online:
                self.state.last_transition = f"offline→online @ {now}"
                logger.info(
                    "Runner reconnected: %s v%s (after %d attempts)",
                    health.platform, health.version, self.state.reconnect_attempts,
                )
                self.state.reconnect_attempts = 0

        except (RunnerOfflineError, Exception) as e:
            # ── Failure — apply backoff ──
            self.state.consecutive_failures += 1
            self.state.error = str(e) if not isinstance(e, RunnerOfflineError) else "Runner unreachable"

            if was_online:
                self.state.last_transition = f"online→offline @ {now}"
                logger.warning("Runner went offline: %s", self.state.error)

            self.state.online = False
            self.state.reconnect_attempts += 1

            # Exponential backoff: base * factor^(failures-1), capped
            self.state.current_interval = min(
                self.base_interval * (_BACKOFF_FACTOR ** (self.state.consecutive_failures - 1)),
                self.max_interval,
            )
            logger.debug(
                "Runner poll failed (%d consecutive), next check in %.0fs",
                self.state.consecutive_failures,
                self.state.current_interval,
            )
