"""Sims-inspired drive/needs system for agents.

Agents have needs (energy, focus, morale, knowledge) that decay over time
and affect performance. The orchestrator can observe drive states and make
scheduling decisions (e.g. "this agent is low on energy, rotate it out").

Also provides ``DriveRLEnv`` — a gymnasium environment that models drive
recovery as an RL problem — and ``DriveSystem.optimize_via_rl()`` for
running episodes to discover high-effectiveness sequences.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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

    def optimize_via_rl(self, n_episodes: int = 5) -> dict[str, Any]:
        """Run RL episodes to discover high-effectiveness drive sequences.

        Uses ``DriveRLEnv`` if gymnasium is installed; returns an error dict
        otherwise.  The episodes use a random policy as a starter — swap in
        a stable-baselines3 PPO agent for a trained policy.

        Returns a summary dict with the best episode reward found.
        """
        if not _GYMNASIUM_AVAILABLE or DriveRLEnv is None:
            return {"error": "gymnasium not installed — run: pip install gymnasium"}

        env = DriveRLEnv()
        best_reward = 0.0
        best_sequence: list[str] = []

        for _ in range(n_episodes):
            obs, _ = env.reset()
            ep_reward = 0.0
            sequence: list[str] = []
            done = False
            while not done:
                action = env.action_space.sample()
                obs, reward, terminated, truncated, _ = env.step(int(action))
                ep_reward += float(reward)
                sequence.append(DriveRLEnv.ACTION_EVENTS[int(action)])
                done = terminated or truncated
            if ep_reward > best_reward:
                best_reward = ep_reward
                best_sequence = sequence[:]

        return {
            "best_episode_reward": round(best_reward, 3),
            "best_sequence": best_sequence,
            "episodes": n_episodes,
        }

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


# ---------------------------------------------------------------------------
# gymnasium RL environment (optional — guarded by try/import)
# ---------------------------------------------------------------------------

_GYMNASIUM_AVAILABLE = False
DriveRLEnv = None  # type: ignore[assignment]

try:
    import gymnasium as gym
    import numpy as np
    from gymnasium import spaces

    class DriveRLEnv(gym.Env):  # type: ignore[no-redef]
        """gymnasium Env modelling drive recovery as an RL problem.

        Observation space: [energy, focus, morale, knowledge] in [0, 100].
        Action space:      Discrete(4) — one of the recovery events below.
        Reward:            overall_effectiveness() at each step (0–1).
        Episode length:    20 steps (configurable via ``max_steps``).

        Extend with stable-baselines3 PPO for a trained policy::

            from stable_baselines3 import PPO
            model = PPO("MlpPolicy", DriveRLEnv(), verbose=1)
            model.learn(total_timesteps=10_000)
        """

        metadata: dict[str, Any] = {"render_modes": []}

        # Maps action index → recovery event name
        ACTION_EVENTS: list[str] = ["rest", "task_success", "learning", "context_switch"]

        def __init__(self, max_steps: int = 20) -> None:
            super().__init__()
            self.max_steps = max_steps
            self.observation_space = spaces.Box(
                low=0.0, high=100.0, shape=(4,), dtype=np.float32
            )
            self.action_space = spaces.Discrete(len(self.ACTION_EVENTS))
            self._drives = DriveSystem()
            self._steps = 0

        def reset(
            self,
            *,
            seed: int | None = None,
            options: dict[str, Any] | None = None,
        ) -> tuple[Any, dict[str, Any]]:
            super().reset(seed=seed)
            self._drives.reset()
            self._steps = 0
            return self._obs(), {}

        def step(self, action: int) -> tuple[Any, float, bool, bool, dict[str, Any]]:
            event = self.ACTION_EVENTS[int(action)]
            self._drives.apply_event(event)
            self._drives.tick(minutes_worked=0.5)
            self._steps += 1
            obs = self._obs()
            reward = float(self._drives.state.overall_effectiveness())
            terminated = self._steps >= self.max_steps
            return obs, reward, terminated, False, {}

        def _obs(self) -> Any:
            s = self._drives.state
            return np.array(
                [
                    s.levels[DriveType.ENERGY],
                    s.levels[DriveType.FOCUS],
                    s.levels[DriveType.MORALE],
                    s.levels[DriveType.KNOWLEDGE],
                ],
                dtype=np.float32,
            )

    _GYMNASIUM_AVAILABLE = True

except ImportError:
    pass  # gymnasium / numpy optional — DriveRLEnv remains None
