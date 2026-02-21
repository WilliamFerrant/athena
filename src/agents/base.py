"""Base agent class with personality, drives, memory, and token-tracked LLM calls."""

from __future__ import annotations

import logging
from typing import Any

from src.agents.sims.drives import DriveSystem
from src.agents.sims.personality import Personality
from src.memory.context_assembler import ContextAssembler, ConversationCompressor
from src.memory.mem0_client import AgentMemory
from src.safety.injection_guard import assert_safe
from src.token_tracker.tracker import TokenTracker

logger = logging.getLogger(__name__)

# Maximum messages kept in conversation history (user+assistant pairs).
# Older messages are dropped to prevent token cost from growing unbounded.
# mem0 retains the important facts from dropped messages.
MAX_CONVERSATION_MESSAGES = 20  # 10 exchanges


class BaseAgent:
    """Abstract base for all agents in the system.

    Combines:
    - Anthropic SDK calls (via TokenTracker)
    - Persistent memory (via mem0)
    - Sims personality + drive system

    Optionally accepts an ``llm_backend`` (e.g. ``OpenAIBackend``) that
    overrides the default Claude CLI tracker for LLM calls while keeping
    the tracker around for budget / summary bookkeeping.
    """

    agent_type: str = "base"
    default_model: str | None = None  # override per subclass

    def __init__(
        self,
        agent_id: str,
        tracker: TokenTracker,
        memory: AgentMemory | None = None,
        project_memory: AgentMemory | None = None,
        personality: Personality | None = None,
        drive_system: DriveSystem | None = None,
        llm_backend: Any | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.tracker = tracker
        self.memory = memory            # global / user-profile memory (no project scope)
        self.project_memory = project_memory  # per-project memory (project-scoped)
        self.personality = personality or Personality()
        self.drives = drive_system or DriveSystem()
        self._conversation: list[dict[str, Any]] = []
        # If an alternate LLM backend is provided (e.g. OpenAIBackend),
        # use it for LLM calls instead of the Claude CLI tracker.
        self._llm_backend = llm_backend
        # Tiered context assembly (rebuilds prompt from scratch each turn)
        self._context_assembler = ContextAssembler()
        self._conversation_compressor = ConversationCompressor()
        self._conversation_summary: str = ""

    # -- system prompt ---------------------------------------------------------

    def system_prompt(self, task_context: str = "") -> str:
        """Build the full system prompt using tiered context assembly.

        Uses ContextAssembler to fit everything within a token budget:
        identity > drives > hot memories > task > warm memories > conversation > cold refs
        """
        # Identity = role description + personality
        role = self._role_description()
        personality_text = self.personality.to_prompt_fragment()
        identity = role
        if personality_text:
            identity += "\n\n" + personality_text

        # Drive state
        drive_text = self.drives.to_prompt_fragment()

        # Memory tiers — try to load curated memories if curator available
        hot_memories: list[str] = []
        warm_memories: list[str] = []
        cold_references: list[str] = []

        try:
            from src.memory.curator import MemoryCurator, MemoryTier, MemoryCurationStore
            store = MemoryCurationStore()
            # Hot memories — always included
            hot = store.list_by_tier(self.agent_id, MemoryTier.HOT)
            hot_memories = [m.content for m in hot]
            # Warm memories — contextually relevant
            warm = store.list_by_tier(self.agent_id, MemoryTier.WARM)
            warm_memories = [m.content for m in warm[:15]]  # cap to prevent overflow
            # Cold references
            cold = store.list_by_tier(self.agent_id, MemoryTier.COLD)
            cold_references = [m.content for m in cold[:5]]
        except Exception:
            # Fallback to raw mem0 search
            active_memory = self.project_memory or self.memory
            if active_memory and task_context:
                mem_ctx = active_memory.get_relevant_context(task_context)
                if mem_ctx:
                    warm_memories = [mem_ctx]

        # For Athena (manager): also inject global user-profile memory
        if self.project_memory and self.memory and task_context:
            try:
                global_ctx = self.memory.get_relevant_context(task_context, limit=3)
                if global_ctx:
                    hot_memories.insert(0, "Global user context:\n" + global_ctx)
            except Exception:
                pass

        # Use context assembler for budget-aware prompt construction
        return self._context_assembler.assemble(
            identity_text=identity,
            drives_text=drive_text,
            hot_memories=hot_memories,
            warm_memories=warm_memories,
            conversation_summary=self._conversation_summary,
            task_context=task_context,
            cold_references=cold_references,
        )

    def _role_description(self) -> str:
        """Override in subclasses to provide agent-specific role description."""
        return f"You are a {self.agent_type} agent (id: {self.agent_id})."

    # -- LLM calls -------------------------------------------------------------

    @property
    def llm_backend(self):
        """The LLM backend used for chat calls (OpenAI, Claude CLI, etc.)."""
        return self._llm_backend or self.tracker

    def chat(
        self,
        user_message: str,
        task_context: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """Send a message and get a text response."""
        assert_safe(user_message)
        self._conversation.append({"role": "user", "content": user_message})

        # Compress old conversation instead of raw truncation
        if len(self._conversation) > MAX_CONVERSATION_MESSAGES:
            self._conversation_summary, self._conversation = \
                self._conversation_compressor.compress(
                    self._conversation,
                    existing_summary=self._conversation_summary,
                    max_recent=MAX_CONVERSATION_MESSAGES // 2,
                )

        # Tick the drive system (simulate work)
        self.drives.tick(minutes_worked=0.5)

        response = self.llm_backend.create_message(
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

        # Store in memory: prefer project_memory when set, else global memory
        target_memory = self.project_memory or self.memory
        if target_memory and assistant_text:
            try:
                target_memory.add_conversation(self._conversation[-2:])
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
        assert_safe(user_message)
        self._conversation.append({"role": "user", "content": user_message})

        # Compress old conversation instead of raw truncation
        if len(self._conversation) > MAX_CONVERSATION_MESSAGES:
            self._conversation_summary, self._conversation = \
                self._conversation_compressor.compress(
                    self._conversation,
                    existing_summary=self._conversation_summary,
                    max_recent=MAX_CONVERSATION_MESSAGES // 2,
                )

        self.drives.tick(minutes_worked=0.5)

        response = await self.llm_backend.acreate_message(
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

        target_memory = self.project_memory or self.memory
        if target_memory and assistant_text:
            try:
                target_memory.add_conversation(self._conversation[-2:])
            except Exception:
                logger.debug("Memory storage failed for %s", self.agent_id)

        return assistant_text

    # -- lifecycle -------------------------------------------------------------

    def reset_conversation(self) -> None:
        self._conversation = []

    def status(self) -> dict[str, Any]:
        backend_name = type(self.llm_backend).__name__
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "llm_backend": backend_name,
            "personality": self.personality.name,
            "drives": self.drives.state.to_dict(),
            "conversation_length": len(self._conversation),
            "token_usage": self.llm_backend.agent_summary(self.agent_id),
        }
