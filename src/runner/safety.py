"""Safety layer — command blocklist, branch protection, destructive-op guards."""

from __future__ import annotations

import re

# ── Blocked patterns ─────────────────────────────────────────────────────────

# Git push to protected branches
_PROTECTED_BRANCH_PUSH = re.compile(
    r"git\s+push\s+\S+\s+(main|master)\b", re.IGNORECASE
)

# Git merge into protected branches
_PROTECTED_MERGE = re.compile(
    r"git\s+merge\s+.*(main|master)", re.IGNORECASE
)

# Destructive filesystem commands
_DESTRUCTIVE_FS = re.compile(
    r"(rm\s+-rf\s+[/\\]|rmdir\s+/s|del\s+/s|format\s+[a-z]:)", re.IGNORECASE
)

# Deploy commands (should never be automated)
_DEPLOY = re.compile(
    r"(vercel\s+--prod|fly\s+deploy|docker\s+push|kubectl\s+apply)", re.IGNORECASE
)


class SafetyError(Exception):
    """Raised when a command or action violates safety rules."""


def validate_command(command: str) -> None:
    """Check a command against the blocklist. Raises SafetyError if blocked."""
    if _PROTECTED_BRANCH_PUSH.search(command):
        raise SafetyError(
            "Blocked: pushing directly to main/master is not allowed. "
            "Use a feature branch and create a PR instead."
        )
    if _PROTECTED_MERGE.search(command):
        raise SafetyError(
            "Blocked: merging to main/master is not allowed via runner. "
            "Use GitHub PR review workflow."
        )
    if _DESTRUCTIVE_FS.search(command):
        raise SafetyError(
            "Blocked: destructive filesystem command detected. "
            "This operation is not permitted via remote runner."
        )
    if _DEPLOY.search(command):
        raise SafetyError(
            "Blocked: deployment commands are not allowed via runner. "
            "Deploy manually or through CI/CD."
        )


def validate_branch_for_push(branch: str) -> None:
    """Ensure we never push directly to a protected branch."""
    normalized = branch.strip().lower()
    if normalized in ("main", "master"):
        raise SafetyError(
            f"Blocked: cannot push to protected branch '{branch}'. "
            "Create a feature branch (e.g. cla/feature-name) instead."
        )


def validate_no_merge(action: str) -> None:
    """Ensure the action doesn't involve merging."""
    if "merge" in action.lower():
        raise SafetyError("Blocked: merge operations are not allowed via runner.")
