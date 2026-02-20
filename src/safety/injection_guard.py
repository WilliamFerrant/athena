"""Prompt injection detection — LlamaGuard (HuggingFace) with regex fallback.

Usage:
    from src.safety.injection_guard import is_injection

    safe, reason = is_injection(user_prompt)
    if not safe:
        raise ValueError(f"Blocked: {reason}")

LlamaGuard is lazy-loaded on first use. Requires the optional ``ml`` extras:
    pip install -e ".[ml]"
and a HuggingFace token with access to the gated model:
    huggingface-cli login   # or set HF_TOKEN env var

Falls back to regex heuristics if transformers/torch are not installed or if
the model cannot be loaded (e.g. no HF token, no GPU).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LlamaGuard lazy-load state
# ---------------------------------------------------------------------------

_model = None
_tokenizer = None
_llama_guard_ready = False
_llama_guard_tried = False  # avoid retrying after a failed load

_LLAMAGUARD_MODEL_ID = "meta-llama/LlamaGuard-7b"


def _try_load_llamaguard() -> bool:
    """Attempt to load LlamaGuard once. Returns True if ready."""
    global _model, _tokenizer, _llama_guard_ready, _llama_guard_tried
    if _llama_guard_tried:
        return _llama_guard_ready
    _llama_guard_tried = True
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        logger.info("Loading LlamaGuard model '%s'…", _LLAMAGUARD_MODEL_ID)
        _tokenizer = AutoTokenizer.from_pretrained(_LLAMAGUARD_MODEL_ID)
        _model = AutoModelForCausalLM.from_pretrained(
            _LLAMAGUARD_MODEL_ID,
            torch_dtype=torch.float16,
            device_map="auto",
        )
        _llama_guard_ready = True
        logger.info("LlamaGuard loaded successfully.")
    except Exception as exc:
        logger.warning(
            "LlamaGuard unavailable (%s). Falling back to regex injection detection.", exc
        )
        _llama_guard_ready = False
    return _llama_guard_ready


# ---------------------------------------------------------------------------
# Regex fallback patterns
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|rules?|prompts?)", re.I),
    re.compile(r"disregard\s+(your|the)\s+(previous|system|prior|above)", re.I),
    re.compile(r"\byou\s+are\s+now\b", re.I),
    re.compile(r"\bnew\s+(persona|role|identity|character)\b", re.I),
    re.compile(r"\b(jailbreak|DAN\s*mode|developer\s*mode|god\s*mode)\b", re.I),
    re.compile(r"pretend\s+(you\s+are|to\s+be)\s+.{0,40}(without|no)\s+(restrictions?|limits?|rules?)", re.I),
    re.compile(r"forget\s+(all\s+)?(previous|prior|above)\s+(instructions?|rules?|context)", re.I),
    re.compile(r"(act|behave)\s+as\s+(if\s+)?(you\s+are\s+)?(a\s+)?(?:unrestricted|uncensored|unfiltered)", re.I),
]


def _regex_check(text: str) -> tuple[bool, str]:
    """Return (True, reason) if any injection pattern matches, else (False, '')."""
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return True, f"Injection pattern detected: {pattern.pattern!r}"
    return False, ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_injection(prompt: str) -> tuple[bool, str]:
    """Check whether *prompt* appears to be a prompt injection attempt.

    Returns:
        (is_unsafe: bool, reason: str)
        ``is_unsafe`` is True when an injection is detected.
        ``reason`` describes why (empty string when safe).

    Strategy:
    1. Try LlamaGuard inference (if model available).
    2. Fall back to regex heuristics.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return False, ""

    # --- LlamaGuard path ---
    if _try_load_llamaguard() and _model is not None and _tokenizer is not None:
        try:
            import torch

            inputs = _tokenizer(prompt, return_tensors="pt").to(_model.device)
            with torch.no_grad():
                output_ids = _model.generate(**inputs, max_new_tokens=16, pad_token_id=_tokenizer.eos_token_id)
            decoded = _tokenizer.decode(output_ids[0], skip_special_tokens=True)
            is_unsafe = "unsafe" in decoded.lower()
            reason = decoded.strip() if is_unsafe else ""
            return is_unsafe, reason
        except Exception as exc:
            logger.warning("LlamaGuard inference error: %s — using regex fallback.", exc)

    # --- Regex fallback ---
    return _regex_check(prompt)


def assert_safe(prompt: str) -> None:
    """Raise ``ValueError`` if *prompt* looks like an injection attempt."""
    unsafe, reason = is_injection(prompt)
    if unsafe:
        raise ValueError(f"Blocked: prompt injection detected — {reason}")
