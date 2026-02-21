"""Telegram bot for bidirectional communication with Athena.

Allows Athena to:
- Send notifications to the user (task completed, needs help, status updates)
- Receive messages from the user (commands, approvals, quick chat)

Uses the Telegram Bot API directly via httpx (no heavy deps).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

# Telegram API base
TELEGRAM_API = "https://api.telegram.org/bot{token}"


class TelegramNotifier:
    """Sends notifications to the user via Telegram.

    Used by the heartbeat scheduler and other systems to proactively
    reach the user when something important happens.
    """

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
    ) -> None:
        self.bot_token = bot_token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id
        self._client = httpx.AsyncClient(timeout=10)
        self._enabled = bool(self.bot_token and self.chat_id)

        if self._enabled:
            logger.info("Telegram notifier enabled (chat_id=%s)", self.chat_id)
        else:
            logger.info("Telegram notifier disabled (no bot_token/chat_id)")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Send a text message to the configured chat."""
        if not self._enabled:
            logger.debug("Telegram: skipping send (not configured)")
            return False

        url = f"{TELEGRAM_API.format(token=self.bot_token)}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }

        try:
            resp = await self._client.post(url, json=payload)
            if resp.status_code == 200:
                logger.debug("Telegram: message sent")
                return True
            else:
                logger.warning("Telegram send failed: %d %s", resp.status_code, resp.text[:200])
                return False
        except Exception:
            logger.exception("Telegram send error")
            return False

    def send_sync(self, text: str) -> bool:
        """Synchronous wrapper for sending messages (used in callbacks)."""
        if not self._enabled:
            return False
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.send_message(text))
                return True
            else:
                return loop.run_until_complete(self.send_message(text))
        except RuntimeError:
            # No event loop — create one
            return asyncio.run(self.send_message(text))

    # -- Formatted messages ----------------------------------------------------

    async def notify_task_started(self, task_title: str, reason: str) -> bool:
        """Notify that Athena picked up a task autonomously."""
        text = (
            f"🤖 *Athena — Autonomous Action*\n\n"
            f"Started: *{self._escape(task_title)}*\n"
            f"Reason: {self._escape(reason)}"
        )
        return await self.send_message(text)

    async def notify_task_completed(self, task_title: str, summary: str = "") -> bool:
        """Notify that a task was completed."""
        text = f"✅ *Task Completed*\n\n*{self._escape(task_title)}*"
        if summary:
            text += f"\n\n{self._escape(summary[:500])}"
        return await self.send_message(text)

    async def notify_task_failed(self, task_title: str, error: str = "") -> bool:
        """Notify that a task failed."""
        text = f"❌ *Task Failed*\n\n*{self._escape(task_title)}*"
        if error:
            text += f"\n\nError: `{self._escape(error[:300])}`"
        return await self.send_message(text)

    async def notify_needs_help(self, question: str) -> bool:
        """Athena needs user input on something."""
        text = (
            f"💬 *Athena needs your help*\n\n"
            f"{self._escape(question)}"
        )
        return await self.send_message(text)

    async def notify_status(self, drives: dict[str, Any], backlog_count: int) -> bool:
        """Send a status update."""
        text = (
            f"📊 *Athena Status*\n\n"
            f"Energy: {drives.get('energy', '?')}\n"
            f"Focus: {drives.get('focus', '?')}\n"
            f"Morale: {drives.get('morale', '?')}\n"
            f"Status: {drives.get('status', '?')}\n"
            f"Backlog: {backlog_count} tasks"
        )
        return await self.send_message(text)

    async def notify_health_alert(self, project_id: str, check_id: str, status: str) -> bool:
        """Notify about a health check failure."""
        text = (
            f"🚨 *Health Alert*\n\n"
            f"Project: `{self._escape(project_id)}`\n"
            f"Check: `{self._escape(check_id)}`\n"
            f"Status: *{self._escape(status)}*"
        )
        return await self.send_message(text)

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _escape(text: str) -> str:
        """Escape Markdown special chars for Telegram."""
        for char in ("_", "*", "`", "["):
            text = text.replace(char, f"\\{char}")
        return text


class TelegramPoller:
    """Polls for incoming messages from the user via Telegram.

    Allows the user to send commands to Athena from their phone:
    - /status — get current status
    - /tasks — list backlog
    - /approve <task_id> — approve a task in review
    - Any text — forwarded to Athena as a chat message
    """

    def __init__(
        self,
        bot_token: str = "",
        on_message: Callable[[str, str], Any] | None = None,
        on_command: Callable[[str, list[str]], Any] | None = None,
    ) -> None:
        """
        Args:
            bot_token: Telegram bot API token
            on_message: callback(chat_id, text) for regular messages
            on_command: callback(command, args) for /commands
        """
        self.bot_token = bot_token or settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self.on_message = on_message
        self.on_command = on_command
        self._client = httpx.AsyncClient(timeout=30)
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._last_update_id = 0
        self._enabled = bool(self.bot_token and self.chat_id)

    async def start(self) -> None:
        if not self._enabled or self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name="telegram-poller"
        )
        logger.info("Telegram poller started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._client.aclose()
        logger.info("Telegram poller stopped")

    async def _poll_loop(self) -> None:
        """Long-poll for updates from Telegram."""
        while self._running:
            try:
                url = f"{TELEGRAM_API.format(token=self.bot_token)}/getUpdates"
                params = {
                    "offset": self._last_update_id + 1,
                    "timeout": 20,
                }
                resp = await self._client.get(url, params=params)
                if resp.status_code != 200:
                    logger.warning("Telegram poll error: %d", resp.status_code)
                    await asyncio.sleep(5)
                    continue

                data = resp.json()
                for update in data.get("result", []):
                    self._last_update_id = update["update_id"]
                    await self._handle_update(update)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Telegram poll error")
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        """Process a single Telegram update."""
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip()

        # Security: only accept messages from the configured chat
        if chat_id != self.chat_id:
            logger.warning("Telegram: ignoring message from unknown chat %s", chat_id)
            return

        if not text:
            return

        # Commands
        if text.startswith("/"):
            parts = text.split()
            command = parts[0].lower().lstrip("/")
            args = parts[1:]
            logger.info("Telegram command: /%s %s", command, args)
            if self.on_command:
                try:
                    self.on_command(command, args)
                except Exception:
                    logger.exception("Telegram command handler error")
            return

        # Regular message → forward to Athena
        logger.info("Telegram message from user: %s", text[:100])
        if self.on_message:
            try:
                self.on_message(chat_id, text)
            except Exception:
                logger.exception("Telegram message handler error")
