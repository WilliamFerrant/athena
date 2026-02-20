"""Proactive notifications — Slack and Telegram webhooks.

Fires notifications on:
- Health check status transitions (up → down, down → up)
- Task completion / failure
- Rate-limit warnings (>80% usage)
- Runner online/offline transitions

All webhook calls are non-blocking (fire-and-forget via httpx async).
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


class NotifyLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    RECOVERY = "recovery"


# Emoji/icon mapping
_EMOJI = {
    NotifyLevel.INFO: "ℹ️",
    NotifyLevel.WARNING: "⚠️",
    NotifyLevel.CRITICAL: "🔴",
    NotifyLevel.RECOVERY: "✅",
}


class NotificationManager:
    """Central dispatcher for Slack / Telegram notifications."""

    def __init__(
        self,
        slack_webhook: str = "",
        telegram_token: str = "",
        telegram_chat_id: str = "",
    ) -> None:
        self.slack_webhook = slack_webhook or settings.slack_webhook_url
        self.telegram_token = telegram_token or settings.telegram_bot_token
        self.telegram_chat_id = telegram_chat_id or settings.telegram_chat_id
        self._enabled = bool(self.slack_webhook or self.telegram_token)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "slack_configured": bool(self.slack_webhook),
            "telegram_configured": bool(self.telegram_token and self.telegram_chat_id),
        }

    # -- High-level notification methods ------------------------------------

    async def notify_health_change(
        self,
        project_id: str,
        check_id: str,
        old_status: str,
        new_status: str,
        message: str = "",
    ) -> None:
        """Notify on health check status transition."""
        if new_status == "down":
            level = NotifyLevel.CRITICAL
        elif new_status == "degraded":
            level = NotifyLevel.WARNING
        elif new_status == "up" and old_status in ("down", "degraded"):
            level = NotifyLevel.RECOVERY
        else:
            return  # Don't notify on up → up

        text = (
            f"{_EMOJI[level]} *Health Alert*\n"
            f"Project: `{project_id}` / Check: `{check_id}`\n"
            f"Status: {old_status} → *{new_status}*\n"
        )
        if message:
            text += f"Detail: {message}\n"

        await self._send(text, level)

    async def notify_task_complete(
        self,
        task: str,
        subtask_count: int,
        success: bool = True,
    ) -> None:
        """Notify on orchestrator task completion."""
        level = NotifyLevel.INFO if success else NotifyLevel.WARNING
        status = "completed" if success else "failed"
        text = (
            f"{_EMOJI[level]} *Task {status}*\n"
            f"Task: {task[:200]}\n"
            f"Subtasks: {subtask_count}\n"
        )
        await self._send(text, level)

    async def notify_rate_limit_warning(
        self,
        window: str,
        pct_used: float,
        tokens_used: int,
        tokens_cap: int,
    ) -> None:
        """Notify when approaching rate limits."""
        level = NotifyLevel.CRITICAL if pct_used >= 95 else NotifyLevel.WARNING
        text = (
            f"{_EMOJI[level]} *Rate Limit Warning*\n"
            f"Window: {window}\n"
            f"Usage: {pct_used:.1f}% ({tokens_used:,} / {tokens_cap:,} tokens)\n"
        )
        await self._send(text, level)

    async def notify_runner_transition(
        self,
        online: bool,
        version: str | None = None,
    ) -> None:
        """Notify on runner online/offline transition."""
        if online:
            level = NotifyLevel.RECOVERY
            text = f"{_EMOJI[level]} *Runner Online* — v{version or '?'}\n"
        else:
            level = NotifyLevel.CRITICAL
            text = f"{_EMOJI[level]} *Runner Offline* — tunnel disconnected\n"
        await self._send(text, level)

    async def send_custom(self, text: str, level: NotifyLevel = NotifyLevel.INFO) -> None:
        """Send a custom notification."""
        await self._send(f"{_EMOJI[level]} {text}", level)

    # -- Low-level dispatch -------------------------------------------------

    async def _send(self, text: str, level: NotifyLevel) -> None:
        """Dispatch to all configured channels (fire-and-forget)."""
        if not self._enabled:
            return
        tasks = []
        if self.slack_webhook:
            tasks.append(self._send_slack(text))
        if self.telegram_token and self.telegram_chat_id:
            tasks.append(self._send_telegram(text))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_slack(self, text: str) -> None:
        """POST to Slack incoming webhook."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self.slack_webhook,
                    json={"text": text, "mrkdwn": True},
                )
                if resp.status_code != 200:
                    logger.warning("Slack webhook returned %d: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Slack notification failed: %s", exc)

    async def _send_telegram(self, text: str) -> None:
        """POST to Telegram Bot API."""
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": self.telegram_chat_id,
                        "text": text,
                        "parse_mode": "Markdown",
                    },
                )
                if resp.status_code != 200:
                    logger.warning("Telegram API returned %d: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Telegram notification failed: %s", exc)


# -- Singleton -----------------------------------------------------------------

_notifier: NotificationManager | None = None


def get_notifier() -> NotificationManager:
    """Return the process-level notification manager."""
    global _notifier
    if _notifier is None:
        _notifier = NotificationManager()
    return _notifier
