"""Discord bot for bidirectional communication with Athena.

Allows Athena to:
- Send notifications to the user via a Discord webhook or bot
- Receive messages from the user (commands, approvals, quick chat)

Two modes:
1. **Webhook only** (DISCORD_WEBHOOK_URL) — push notifications, no receive
2. **Bot mode** (DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID) — full bidirectional

Uses discord.py (py-cord) for the bot gateway, httpx for webhooks.
Falls back to webhook-only if discord.py is not installed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Sends notifications to the user via Discord webhook.

    Works without any extra dependencies — just httpx POST to a webhook URL.
    Used by the heartbeat scheduler, health alerts, etc.
    """

    def __init__(
        self,
        webhook_url: str = "",
    ) -> None:
        self.webhook_url = webhook_url or settings.discord_webhook_url
        self._client = httpx.AsyncClient(timeout=10)
        self._enabled = bool(self.webhook_url)

        if self._enabled:
            logger.info("Discord notifier enabled (webhook)")
        else:
            logger.info("Discord notifier disabled (no webhook_url)")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send_message(self, text: str) -> bool:
        """Send a text message to the configured Discord webhook."""
        if not self._enabled:
            logger.debug("Discord: skipping send (not configured)")
            return False

        payload = {"content": text}

        try:
            resp = await self._client.post(self.webhook_url, json=payload)
            if resp.status_code in (200, 204):
                logger.debug("Discord: message sent")
                return True
            else:
                logger.warning("Discord send failed: %d %s", resp.status_code, resp.text[:200])
                return False
        except Exception:
            logger.exception("Discord send error")
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
            return asyncio.run(self.send_message(text))

    # -- Formatted messages ----------------------------------------------------

    async def notify_task_started(self, task_title: str, reason: str) -> bool:
        """Notify that Athena picked up a task autonomously."""
        text = (
            f"🤖 **Athena — Autonomous Action**\n\n"
            f"Started: **{task_title}**\n"
            f"Reason: {reason}"
        )
        return await self.send_message(text)

    async def notify_task_completed(self, task_title: str, summary: str = "") -> bool:
        """Notify that a task was completed."""
        text = f"✅ **Task Completed**\n\n**{task_title}**"
        if summary:
            text += f"\n\n{summary[:1500]}"
        return await self.send_message(text)

    async def notify_task_failed(self, task_title: str, error: str = "") -> bool:
        """Notify that a task failed."""
        text = f"❌ **Task Failed**\n\n**{task_title}**"
        if error:
            text += f"\n\nError: `{error[:500]}`"
        return await self.send_message(text)

    async def notify_needs_help(self, question: str) -> bool:
        """Athena needs user input on something."""
        text = (
            f"💬 **Athena needs your help**\n\n"
            f"{question}"
        )
        return await self.send_message(text)

    async def notify_status(self, drives: dict[str, Any], backlog_count: int) -> bool:
        """Send a status update."""
        text = (
            f"📊 **Athena Status**\n\n"
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
            f"🚨 **Health Alert**\n\n"
            f"Project: `{project_id}`\n"
            f"Check: `{check_id}`\n"
            f"Status: **{status}**"
        )
        return await self.send_message(text)

    async def close(self) -> None:
        await self._client.aclose()


class DiscordBotPoller:
    """Listens for incoming messages from the user via Discord bot gateway.

    Allows the user to send commands to Athena from Discord:
    - !status — get current status
    - !tasks — list backlog
    - !approve <task_id> — approve a task in review
    - Any text — forwarded to Athena as a chat message

    Requires `discord.py` (pip install discord.py) and a bot token.
    Falls back gracefully if not installed.
    """

    def __init__(
        self,
        bot_token: str = "",
        channel_id: str = "",
        on_message: Callable[[str, str], Any] | None = None,
        on_command: Callable[[str, list[str]], Any] | None = None,
    ) -> None:
        self.bot_token = bot_token or settings.discord_bot_token
        self.channel_id = channel_id or settings.discord_channel_id
        self.on_message = on_message
        self.on_command = on_command
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._client: Any = None  # discord.Client instance
        self._enabled = bool(self.bot_token and self.channel_id)

    async def start(self) -> None:
        if not self._enabled or self._running:
            if not self._enabled:
                logger.info("Discord bot poller disabled (no bot_token/channel_id)")
            return

        try:
            import discord
        except ImportError:
            logger.warning(
                "discord.py not installed — Discord bot poller disabled. "
                "Install with: pip install discord.py"
            )
            self._enabled = False
            return

        self._running = True
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        self._client = client

        channel_id_int = int(self.channel_id)

        @client.event
        async def on_ready():
            logger.info("Discord bot connected as %s", client.user)

        @client.event
        async def on_message(message):
            # Ignore own messages
            if message.author == client.user:
                return
            # Only listen in the configured channel
            if message.channel.id != channel_id_int:
                return

            text = message.content.strip()
            if not text:
                return

            # Commands start with !
            if text.startswith("!"):
                parts = text[1:].split()
                command = parts[0].lower()
                args = parts[1:]
                logger.info("Discord command: !%s %s", command, args)
                if self.on_command:
                    try:
                        result = self.on_command(command, args)
                        # If command handler returns a string, send it back
                        if isinstance(result, str):
                            await message.channel.send(result)
                    except Exception:
                        logger.exception("Discord command handler error")
                return

            # Regular message → forward to Athena
            logger.info("Discord message from %s: %s", message.author, text[:100])
            if self.on_message:
                try:
                    result = self.on_message(str(message.channel.id), text)
                    # If message handler returns a reply, send it
                    if isinstance(result, str):
                        # Split long messages (Discord 2000 char limit)
                        for chunk in _chunk_message(result):
                            await message.channel.send(chunk)
                except Exception:
                    logger.exception("Discord message handler error")

        # Run the bot in the background
        self._task = asyncio.create_task(
            client.start(self.bot_token), name="discord-bot"
        )
        logger.info("Discord bot poller started (channel=%s)", self.channel_id)

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Discord bot poller stopped")

    async def send_to_channel(self, text: str) -> bool:
        """Send a message directly to the configured channel via bot."""
        if not self._client or not self._running:
            return False
        try:
            channel = self._client.get_channel(int(self.channel_id))
            if channel:
                for chunk in _chunk_message(text):
                    await channel.send(chunk)
                return True
        except Exception:
            logger.exception("Discord bot send error")
        return False


def _chunk_message(text: str, limit: int = 1990) -> list[str]:
    """Split a message into chunks that fit Discord's 2000 char limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find last newline before limit
        cut = text.rfind("\n", 0, limit)
        if cut < limit * 0.5:
            cut = limit  # no good newline, hard cut
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks
