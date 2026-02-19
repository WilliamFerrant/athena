"""Base agent class with personality, drives, memory, and token-tracked LLM calls."""

from __future__ import annotations

import logging
from typing import Any

from src.agents.sims.drives import DriveSystem
from src.agents.sims.personality import Personality
from src.memory.mem0_client import AgentMemory
from src.token_tracker.tracker import TokenTracker

logger = logging.getLogger(__name__)


class BaseAgent:
    """Abstract base for all agents in the system.

    Combines:
    - Anthropic SDK calls (via TokenTracker)
    - Persistent memory (via mem0)
    - Sims personality + drive system
    """

    agent_type: str = "base"
    default_model: str | None = None  # override per subclass

    def __init__(
        self,
        agent_id: str,
        tracker: TokenTracker,
        memory: AgentMemory | None = None,
        personality: Personality | None = None,
        drive_system: DriveSystem | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.tracker = tracker
        self.memory = memory
        self.personality = personality or Personality()
        self.drives = drive_system or DriveSystem()
        self._conversation: list[dict[str, Any]] = []

    # -- system prompt ---------------------------------------------------------

    def system_prompt(self, task_context: str = "") -> str:
        """Build the full system prompt including personality, drives, and memory."""
        parts: list[str] = []

        # Role description
        role = self._role_description()
        if role:
            parts.append(role)

        # Personality injection
        personality_text = self.personality.to_prompt_fragment()
        if personality_text:
            parts.append(personality_text)

        # Drive state injection
        drive_text = self.drives.to_prompt_fragment()
        if drive_text:
            parts.append(drive_text)

        # Memory context
        if self.memory and task_context:
            mem_ctx = self.memory.get_relevant_context(task_context)
            if mem_ctx:
                parts.append(mem_ctx)

        return "\n\n".join(parts)

    def _role_description(self) -> str:
        """Override in subclasses to provide agent-specific role description."""
        return f"You are a {self.agent_type} agent (id: {self.agent_id})."

    # -- LLM calls -------------------------------------------------------------

    def chat(
        self,
        user_message: str,
        task_context: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """Send a message and get a text response."""
        self._conversation.append({"role": "user", "content": user_message})

        # Tick the drive system (simulate work)
        self.drives.tick(minutes_worked=0.5)

        response = self.tracker.create_message(
            agent_id=self.agent_id,
            model=self.default_model,
            system=self.system_prompt(task_context),
            messages=self._conversation,
            tools=tools,
            max_tokens=max_tokens,
        )

        # Extract text from response
        assistant_text = ""
        for block in response.content:
            if block.type == "text":
                assistant_text += block.text

        self._conversation.append({"role": "assistant", "content": assistant_text})

        # Store in memory if available
        if self.memory and assistant_text:
            try:
                self.memory.add_conversation(self._conversation[-2:])
            except Exception:
                logger.debug("Memory storage failed for %s", self.agent_id)

        return assistant_text

    async def achat(
        self,
        user_message: str,
        task_context: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """Async version of chat."""
        self._conversation.append({"role": "user", "content": user_message})
        self.drives.tick(minutes_worked=0.5)

        response = await self.tracker.acreate_message(
            agent_id=self.agent_id,
            model=self.default_model,
            system=self.system_prompt(task_context),
            messages=self._conversation,
            tools=tools,
            max_tokens=max_tokens,
        )

        assistant_text = ""
        for block in response.content:
            if block.type == "text":
                assistant_text += block.text

        self._conversation.append({"role": "assistant", "content": assistant_text})

        if self.memory and assistant_text:
            try:
                self.memory.add_conversation(self._conversation[-2:])
            except Exception:
                logger.debug("Memory storage failed for %s", self.agent_id)

        return assistant_text

    # -- lifecycle -------------------------------------------------------------

    def reset_conversation(self) -> None:
        self._conversation = []

    def status(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "personality": self.personality.name,
            "drives": self.drives.state.to_dict(),
            "conversation_length": len(self._conversation),
            "token_usage": self.tracker.agent_summary(self.agent_id),
        }
