"""Sims-inspired drive/needs system for agents.

Agents have needs (energy, focus, morale, knowledge) that decay over time
and affect performance. The orchestrator can observe drive states and make
scheduling decisions (e.g. "this agent is low on energy, rotate it out").
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class DriveType(str, Enum):
    ENERGY = "energy"          # Depleted by work, restored by rest
    FOCUS = "focus"            # Depleted by context-switching, restored by single-tasking
    MORALE = "morale"          # Boosted by success, drained by failures/blocks
    KNOWLEDGE = "knowledge"    # Boosted by learning, decays over idle time


# Decay rates per minute of work
DECAY_RATES: dict[DriveType, float] = {
    DriveType.ENERGY: 0.8,     # Loses 0.8 per minute of active work
    DriveType.FOCUS: 1.2,      # Focus drains faster
    DriveType.MORALE: 0.3,     # Morale is relatively stable
    DriveType.KNOWLEDGE: 0.1,  # Knowledge decays very slowly
}

# Recovery amounts per event
RECOVERY: dict[str, dict[DriveType, float]] = {
    "task_success": {
        DriveType.ENERGY: -2.0,     # Completing tasks costs some energy
        DriveType.MORALE: 10.0,     # But boosts morale
        DriveType.KNOWLEDGE: 5.0,   # And builds knowledge
        DriveType.FOCUS: 3.0,
    },
    "task_failure": {
        DriveType.ENERGY: -5.0,
        DriveType.MORALE: -15.0,
        DriveType.FOCUS: -8.0,
        DriveType.KNOWLEDGE: 2.0,   # You still learn from failure
    },
    "rest": {
        DriveType.ENERGY: 30.0,
        DriveType.FOCUS: 20.0,
        DriveType.MORALE: 5.0,
        DriveType.KNOWLEDGE: 0.0,
    },
    "context_switch": {
        DriveType.FOCUS: -20.0,
        DriveType.ENERGY: -3.0,
        DriveType.MORALE: -2.0,
        DriveType.KNOWLEDGE: 0.0,
    },
    "learning": {
        DriveType.KNOWLEDGE: 15.0,
        DriveType.ENERGY: -5.0,
        DriveType.FOCUS: -3.0,
        DriveType.MORALE: 3.0,
    },
}


@dataclass
class DriveState:
    """Current state of all drives for an agent."""

    levels: dict[DriveType, float] = field(default_factory=lambda: {
        DriveType.ENERGY: 100.0,
        DriveType.FOCUS: 100.0,
        DriveType.MORALE: 75.0,
        DriveType.KNOWLEDGE: 50.0,
    })
    last_update: float = field(default_factory=time.time)

    def get(self, drive: DriveType) -> float:
        return self.levels.get(drive, 0.0)

    def overall_effectiveness(self) -> float:
        """0.0–1.0 score of how effective the agent currently is."""
        weights = {
            DriveType.ENERGY: 0.35,
            DriveType.FOCUS: 0.30,
            DriveType.MORALE: 0.25,
            DriveType.KNOWLEDGE: 0.10,
        }
        score = sum(
            (self.levels.get(d, 0.0) / 100.0) * w
            for d, w in weights.items()
        )
        return max(0.0, min(1.0, score))

    def needs_rest(self) -> bool:
        return self.levels[DriveType.ENERGY] < 20.0

    def is_unfocused(self) -> bool:
        return self.levels[DriveType.FOCUS] < 25.0

    def is_demoralized(self) -> bool:
        return self.levels[DriveType.MORALE] < 15.0

    def status_label(self) -> str:
        if self.needs_rest():
            return "exhausted"
        if self.is_demoralized():
            return "demoralized"
        if self.is_unfocused():
            return "distracted"
        eff = self.overall_effectiveness()
        if eff > 0.8:
            return "in the zone"
        if eff > 0.5:
            return "working"
        return "sluggish"

    def to_dict(self) -> dict[str, float | str]:
        return {
            "energy": round(self.levels[DriveType.ENERGY], 1),
            "focus": round(self.levels[DriveType.FOCUS], 1),
            "morale": round(self.levels[DriveType.MORALE], 1),
            "knowledge": round(self.levels[DriveType.KNOWLEDGE], 1),
            "effectiveness": round(self.overall_effectiveness(), 3),
            "status": self.status_label(),
        }


class DriveSystem:
    """Manages the drive lifecycle for a single agent."""

    def __init__(self, state: DriveState | None = None) -> None:
        self.state = state or DriveState()

    def tick(self, minutes_worked: float = 1.0) -> None:
        """Simulate time passing — drives decay."""
        for drive_type, rate in DECAY_RATES.items():
            current = self.state.levels[drive_type]
            self.state.levels[drive_type] = max(0.0, current - rate * minutes_worked)
        self.state.last_update = time.time()

    def apply_event(self, event: str) -> None:
        """Apply a named event's effects to drives."""
        effects = RECOVERY.get(event, {})
        for drive_type, delta in effects.items():
            current = self.state.levels.get(drive_type, 50.0)
            self.state.levels[drive_type] = max(0.0, min(100.0, current + delta))
        self.state.last_update = time.time()

    def rest(self) -> None:
        self.apply_event("rest")

    def record_success(self) -> None:
        self.apply_event("task_success")

    def record_failure(self) -> None:
        self.apply_event("task_failure")

    def context_switch(self) -> None:
        self.apply_event("context_switch")

    def learn(self) -> None:
        self.apply_event("learning")

    def reset(self) -> None:
        """Full reset to starting values."""
        self.state = DriveState()

    def to_prompt_fragment(self) -> str:
        """Describe current state for injection into system prompts."""
        state = self.state
        status = state.status_label()
        eff = state.overall_effectiveness()
        lines = [
            f"Current state: {status} (effectiveness: {eff:.0%})",
            f"  Energy: {state.get(DriveType.ENERGY):.0f}/100",
            f"  Focus: {state.get(DriveType.FOCUS):.0f}/100",
            f"  Morale: {state.get(DriveType.MORALE):.0f}/100",
        ]
        if state.needs_rest():
            lines.append("  ⚠ You are exhausted. Keep responses concise to conserve energy.")
        if state.is_unfocused():
            lines.append("  ⚠ You are losing focus. Avoid context-switching.")
        if state.is_demoralized():
            lines.append("  ⚠ Morale is low. Consider asking for help or simpler tasks.")
        return "\n".join(lines)
