"""Tiered context assembly — rebuilds the system prompt from scratch each turn.

Instead of a simple string concatenation, this module assembles the prompt
with explicit budgets for each section:

1. **Identity** — Who Athena is (fixed, always included)
2. **Drives** — Current Sims drive state (small, always included)
3. **Hot memories** — Critical memories that are always present
4. **Warm memories** — Context-relevant memories, included by relevance
5. **Conversation summary** — Compressed history of earlier conversation
6. **Task context** — Current task details
7. **Cold reference** — Only included if directly queried/relevant

Each section has a token budget. The assembler fits as much as possible
within the total target (~100k tokens, configurable) using tiered priorities.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Approximate tokens per character (conservative for English)
CHARS_PER_TOKEN = 4

# Default budget allocation (in tokens)
DEFAULT_TOTAL_BUDGET = 24_000  # ~24k tokens total system prompt budget

# Budget allocation as percentage of total
BUDGET_ALLOCATION = {
    "identity": 0.15,       # 15% — role description + personality
    "drives": 0.03,         # 3% — current drive state
    "hot_memories": 0.20,   # 20% — critical always-on memories
    "warm_memories": 0.25,  # 25% — contextually relevant memories
    "conversation": 0.20,   # 20% — compressed conversation summary
    "task_context": 0.12,   # 12% — current task details
    "cold_reference": 0.05, # 5% — archived references if space allows
}


@dataclass
class PromptSection:
    """A section of the assembled prompt with its budget."""
    name: str
    content: str = ""
    token_budget: int = 0
    priority: int = 0  # lower = higher priority (0 = must include)
    actual_tokens: int = 0

    @property
    def is_over_budget(self) -> bool:
        return self.actual_tokens > self.token_budget

    def truncate_to_budget(self) -> str:
        """Truncate content to fit within token budget."""
        max_chars = self.token_budget * CHARS_PER_TOKEN
        if len(self.content) <= max_chars:
            return self.content
        # Truncate at last newline before budget
        truncated = self.content[:max_chars]
        last_nl = truncated.rfind("\n")
        if last_nl > max_chars * 0.7:
            truncated = truncated[:last_nl]
        return truncated + "\n[... truncated for context budget]"


def estimate_tokens(text: str) -> int:
    """Rough token estimate from character count."""
    return len(text) // CHARS_PER_TOKEN


class ContextAssembler:
    """Assembles the full system prompt with tiered budgets.

    Usage:
        assembler = ContextAssembler(total_budget=24000)
        prompt = assembler.assemble(
            identity_text="You are Athena...",
            drives_text="Energy: 80, Focus: 65...",
            hot_memories=["User's name is X", ...],
            warm_memories=["Project uses Next.js", ...],
            conversation_summary="Earlier we discussed...",
            task_context="Build the auth system",
            cold_references=["Old decision about DB choice", ...],
        )
    """

    def __init__(self, total_budget: int = DEFAULT_TOTAL_BUDGET) -> None:
        self.total_budget = total_budget

    def assemble(
        self,
        identity_text: str = "",
        drives_text: str = "",
        hot_memories: list[str] | None = None,
        warm_memories: list[str] | None = None,
        conversation_summary: str = "",
        task_context: str = "",
        cold_references: list[str] | None = None,
    ) -> str:
        """Build the complete system prompt within budget.

        Sections are filled in priority order. If a section is under budget,
        its leftover tokens are redistributed to lower-priority sections.
        """
        sections: list[PromptSection] = [
            PromptSection(
                name="identity",
                content=identity_text,
                token_budget=int(self.total_budget * BUDGET_ALLOCATION["identity"]),
                priority=0,
            ),
            PromptSection(
                name="drives",
                content=drives_text,
                token_budget=int(self.total_budget * BUDGET_ALLOCATION["drives"]),
                priority=1,
            ),
            PromptSection(
                name="hot_memories",
                content=self._format_memories("Critical context (always active):", hot_memories),
                token_budget=int(self.total_budget * BUDGET_ALLOCATION["hot_memories"]),
                priority=2,
            ),
            PromptSection(
                name="task_context",
                content=f"Current task:\n{task_context}" if task_context else "",
                token_budget=int(self.total_budget * BUDGET_ALLOCATION["task_context"]),
                priority=3,
            ),
            PromptSection(
                name="warm_memories",
                content=self._format_memories("Relevant context from memory:", warm_memories),
                token_budget=int(self.total_budget * BUDGET_ALLOCATION["warm_memories"]),
                priority=4,
            ),
            PromptSection(
                name="conversation",
                content=f"Conversation context:\n{conversation_summary}" if conversation_summary else "",
                token_budget=int(self.total_budget * BUDGET_ALLOCATION["conversation"]),
                priority=5,
            ),
            PromptSection(
                name="cold_reference",
                content=self._format_memories("Archived references:", cold_references),
                token_budget=int(self.total_budget * BUDGET_ALLOCATION["cold_reference"]),
                priority=6,
            ),
        ]

        # Calculate actual token usage and redistribute surplus
        surplus = 0
        for section in sections:
            section.actual_tokens = estimate_tokens(section.content)
            if section.actual_tokens < section.token_budget:
                surplus += section.token_budget - section.actual_tokens

        # Distribute surplus to sections that need it (by priority order)
        if surplus > 0:
            needy = [s for s in sections if s.actual_tokens > s.token_budget]
            needy.sort(key=lambda s: s.priority)
            for section in needy:
                needed = section.actual_tokens - section.token_budget
                give = min(needed, surplus)
                section.token_budget += give
                surplus -= give

        # Assemble final prompt — truncate each section to its budget
        parts: list[str] = []
        for section in sections:
            if not section.content:
                continue
            text = section.truncate_to_budget()
            if text.strip():
                parts.append(text)

        assembled = "\n\n".join(parts)

        total_tokens = estimate_tokens(assembled)
        logger.debug(
            "Context assembled: %d tokens (%d/%d budget). Sections: %s",
            total_tokens,
            total_tokens,
            self.total_budget,
            {s.name: estimate_tokens(s.content) for s in sections if s.content},
        )

        return assembled

    def _format_memories(self, header: str, memories: list[str] | None) -> str:
        """Format a list of memory strings with a header."""
        if not memories:
            return ""
        lines = [f"- {m}" for m in memories if m.strip()]
        if not lines:
            return ""
        return f"{header}\n" + "\n".join(lines)


class ConversationCompressor:
    """Compresses conversation history into a summary for the context budget.

    Instead of keeping 20 raw messages, this produces a compact summary
    that captures the key decisions, facts, and current direction.
    """

    def __init__(self, llm_fn: Any = None) -> None:
        """
        Args:
            llm_fn: callable(prompt: str) -> str for LLM calls
        """
        self._llm = llm_fn

    def compress(
        self,
        messages: list[dict[str, Any]],
        existing_summary: str = "",
        max_recent: int = 4,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Compress old messages into summary, keep recent ones raw.

        Returns:
            (updated_summary, recent_messages_to_keep)
        """
        if len(messages) <= max_recent:
            return existing_summary, messages

        old_messages = messages[:-max_recent]
        recent = messages[-max_recent:]

        if not self._llm:
            # No LLM available — just keep a text dump of old messages
            old_text = "\n".join(
                f"{m['role']}: {m['content'][:200]}" for m in old_messages
            )
            summary = existing_summary + "\n" + old_text if existing_summary else old_text
            return summary[-4000:], recent  # hard truncate

        # Use LLM to compress
        old_text = "\n".join(
            f"{m['role']}: {m['content']}" for m in old_messages
        )

        prompt = f"""Compress this conversation into a concise summary preserving:
- Key decisions made
- Important facts stated
- Current direction/plan
- Any user preferences or constraints mentioned

Previous summary (if any):
{existing_summary or "(none)"}

New messages to compress:
{old_text}

Write a dense, factual summary (max 500 words). No fluff."""

        try:
            summary = self._llm(prompt)
            return summary.strip(), recent
        except Exception:
            logger.debug("Conversation compression failed, using raw truncation")
            return existing_summary, recent
