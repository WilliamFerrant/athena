"""OpenAI ChatGPT backend — drop-in replacement for Claude CLI calls.

Used by the Manager agent when ``OPENAI_API_KEY`` is configured.
Returns the same ``ClaudeResponse``-shaped objects so ``BaseAgent`` code
needs zero changes to consume the output.

Usage:
    from src.token_tracker.openai_backend import OpenAIBackend

    backend = OpenAIBackend(api_key="sk-...", model="gpt-4o")
    response = backend.create_message(
        agent_id="manager",
        system="You are a senior engineering manager.",
        messages=[{"role": "user", "content": "Plan a landing page"}],
    )
    print(response.content[0].text)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from src.config import settings
from src.token_tracker.tracker import ClaudeResponse, UsageRecord

logger = logging.getLogger(__name__)


class OpenAIBackend:
    """LLM backend that routes calls to OpenAI's ChatGPT API.

    Provides the same ``create_message`` / ``acreate_message`` /
    ``create_message_stream`` interface as ``TokenTracker`` so it can be
    used as a drop-in ``llm_backend`` for any agent.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
    ) -> None:
        try:
            import openai
        except ImportError as exc:
            raise ImportError(
                "The openai package is required for the ChatGPT backend. "
                "Install it with:  pip install openai"
            ) from exc

        self._client = openai.OpenAI(api_key=api_key)
        self._async_client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model
        self.records: list[UsageRecord] = []
        self._call_count: int = 0

    # -- budget helpers (mirror TokenTracker interface) -------------------------

    @property
    def is_over_budget(self) -> bool:
        return self._call_count >= settings.daily_call_limit

    @property
    def budget_remaining(self) -> int:
        return max(0, settings.daily_call_limit - self._call_count)

    def agent_summary(self, agent_id: str) -> dict[str, Any]:
        agent_records = [r for r in self.records if r.agent_id == agent_id]
        return {
            "agent_id": agent_id,
            "backend": "openai",
            "model": self.model,
            "calls": len(agent_records),
            "input_chars": sum(r.input_chars for r in agent_records),
            "output_chars": sum(r.output_chars for r in agent_records),
            "total_cost_usd": 0.0,  # ChatGPT Plus subscription
        }

    # -- core: call OpenAI API -------------------------------------------------

    def create_message(
        self,
        *,
        agent_id: str,
        model: str | None = None,
        max_tokens: int = 4096,
        system: str | None = None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ClaudeResponse:
        """Call OpenAI ChatCompletion and return a ClaudeResponse-shaped object."""
        if self.is_over_budget:
            raise RuntimeError(
                f"Daily call limit of {settings.daily_call_limit} reached "
                f"({self._call_count} calls made)"
            )

        oai_messages = self._build_messages(system, messages)
        use_model = model or self.model

        t0 = time.perf_counter()
        try:
            response = self._client.chat.completions.create(
                model=use_model,
                messages=oai_messages,
                max_tokens=max_tokens,
            )
            output = response.choices[0].message.content or ""
        except Exception as exc:
            logger.error("OpenAI API error: %s", exc)
            output = f"Error: OpenAI API call failed — {exc}"

        latency = (time.perf_counter() - t0) * 1000
        input_chars = sum(len(m.get("content", "")) for m in oai_messages)

        record = UsageRecord(
            agent_id=agent_id,
            model=use_model,
            input_chars=input_chars,
            output_chars=len(output),
            latency_ms=latency,
        )
        self.records.append(record)
        self._call_count += 1

        return ClaudeResponse(text=output, input_chars=input_chars)

    async def acreate_message(
        self,
        *,
        agent_id: str,
        model: str | None = None,
        max_tokens: int = 4096,
        system: str | None = None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ClaudeResponse:
        """Async version using openai.AsyncOpenAI."""
        if self.is_over_budget:
            raise RuntimeError(
                f"Daily call limit of {settings.daily_call_limit} reached "
                f"({self._call_count} calls made)"
            )

        oai_messages = self._build_messages(system, messages)
        use_model = model or self.model

        t0 = time.perf_counter()
        try:
            response = await self._async_client.chat.completions.create(
                model=use_model,
                messages=oai_messages,
                max_tokens=max_tokens,
            )
            output = response.choices[0].message.content or ""
        except Exception as exc:
            logger.error("OpenAI API async error: %s", exc)
            output = f"Error: OpenAI API call failed — {exc}"

        latency = (time.perf_counter() - t0) * 1000
        input_chars = sum(len(m.get("content", "")) for m in oai_messages)

        record = UsageRecord(
            agent_id=agent_id,
            model=use_model,
            input_chars=input_chars,
            output_chars=len(output),
            latency_ms=latency,
        )
        self.records.append(record)
        self._call_count += 1

        return ClaudeResponse(text=output, input_chars=input_chars)

    async def create_message_stream(
        self,
        *,
        agent_id: str,
        model: str | None = None,
        system: str | None = None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ):
        """Async generator — yields SSE-compatible chunks from OpenAI streaming.

        Same event shape as TokenTracker.create_message_stream:
          {"type": "chunk", "data": "..."}
          {"type": "done",  "data": "full text"}
          {"type": "error", "data": "..."}
        """
        if self.is_over_budget:
            yield {"type": "error", "data": "Daily call limit reached"}
            return

        oai_messages = self._build_messages(system, messages)
        use_model = model or self.model

        t0 = time.perf_counter()
        full_output = ""

        try:
            stream = await self._async_client.chat.completions.create(
                model=use_model,
                messages=oai_messages,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    full_output += delta.content
                    yield {"type": "chunk", "data": delta.content}

        except Exception as exc:
            logger.error("OpenAI streaming error: %s", exc)
            yield {"type": "error", "data": f"OpenAI API error: {exc}"}
            return

        latency = (time.perf_counter() - t0) * 1000
        input_chars = sum(len(m.get("content", "")) for m in oai_messages)

        record = UsageRecord(
            agent_id=agent_id,
            model=use_model,
            input_chars=input_chars,
            output_chars=len(full_output),
            latency_ms=latency,
        )
        self.records.append(record)
        self._call_count += 1

        yield {"type": "done", "data": full_output}

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _build_messages(
        system: str | None,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        """Convert system prompt + conversation history to OpenAI message format."""
        oai_messages: list[dict[str, str]] = []

        if system:
            oai_messages.append({"role": "system", "content": system})

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                # Flatten structured content blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif hasattr(block, "text"):
                        text_parts.append(block.text)
                content = "\n".join(text_parts)
            oai_messages.append({"role": role, "content": content})

        return oai_messages
