"""PPO-based drive optimizer using stable-baselines3.

Trains a PPO policy on the DriveRLEnv to learn optimal event sequences
that maximize agent effectiveness. Falls back to random-policy search
if stable-baselines3 is not installed.

Usage::

    from src.agents.sims.ppo_optimizer import train_ppo_policy, run_ppo_episode

    # Train a fresh policy (or load cached)
    model = train_ppo_policy(total_timesteps=10_000)

    # Run an episode with the trained policy
    result = run_ppo_episode(model)
    print(result["reward"], result["sequence"])
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MODEL_CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "rl_models"
_DEFAULT_MODEL_PATH = _MODEL_CACHE_DIR / "drive_ppo"


def train_ppo_policy(
    total_timesteps: int = 10_000,
    save_path: Path | None = None,
    force_retrain: bool = False,
) -> Any:
    """Train a PPO policy on DriveRLEnv.

    Returns a stable_baselines3.PPO model instance.
    Caches the trained model to disk for reuse.
    """
    try:
        from stable_baselines3 import PPO
        from stable_baselines3.common.env_checker import check_env
    except ImportError:
        raise ImportError(
            "stable-baselines3 is required for PPO training. "
            "Install with:  pip install stable-baselines3"
        )

    from src.agents.sims.drives import DriveRLEnv

    if DriveRLEnv is None:
        raise RuntimeError("gymnasium is required for DriveRLEnv")

    save_path = save_path or _DEFAULT_MODEL_PATH
    save_path.parent.mkdir(parents=True, exist_ok=True)

    # Check for cached model
    zip_path = Path(str(save_path) + ".zip")
    if zip_path.exists() and not force_retrain:
        logger.info("Loading cached PPO model from %s", zip_path)
        return PPO.load(str(save_path))

    # Train fresh
    env = DriveRLEnv(max_steps=50)
    logger.info("Training PPO policy (%d timesteps)…", total_timesteps)

    model = PPO(
        "MlpPolicy",
        env,
        verbose=0,
        learning_rate=3e-4,
        n_steps=128,
        batch_size=64,
        n_epochs=10,
        gamma=0.99,
        ent_coef=0.01,
    )
    model.learn(total_timesteps=total_timesteps)

    # Save
    model.save(str(save_path))
    logger.info("PPO model saved to %s", save_path)

    return model


def run_ppo_episode(
    model: Any = None,
    max_steps: int = 50,
) -> dict[str, Any]:
    """Run a single episode with the trained PPO policy.

    Returns an episode summary with reward, action sequence, and final drives.
    """
    from src.agents.sims.drives import DriveRLEnv, DriveType

    if DriveRLEnv is None:
        return {"error": "gymnasium not installed"}

    if model is None:
        try:
            model = train_ppo_policy()
        except ImportError:
            return {"error": "stable-baselines3 not installed"}

    env = DriveRLEnv(max_steps=max_steps)
    obs, _ = env.reset()
    total_reward = 0.0
    sequence: list[str] = []
    done = False

    while not done:
        action, _ = model.predict(obs, deterministic=True)
        obs, reward, terminated, truncated, _ = env.step(int(action))
        total_reward += float(reward)
        sequence.append(DriveRLEnv.ACTION_EVENTS[int(action)])
        done = terminated or truncated

    final_state = env._drives.state
    return {
        "total_reward": round(total_reward, 3),
        "avg_effectiveness": round(total_reward / max_steps, 3),
        "sequence": sequence,
        "final_drives": final_state.to_dict(),
        "steps": len(sequence),
    }


def optimize_drives_ppo(
    n_episodes: int = 5,
    timesteps: int = 10_000,
    force_retrain: bool = False,
) -> dict[str, Any]:
    """High-level function: train (or load) PPO and run evaluation episodes.

    Returns statistics across multiple episodes.
    Falls back to random policy if SB3 is not installed.
    """
    try:
        model = train_ppo_policy(
            total_timesteps=timesteps,
            force_retrain=force_retrain,
        )
    except ImportError:
        # Fallback to random policy from DriveSystem
        from src.agents.sims.drives import DriveSystem
        ds = DriveSystem()
        return {
            "method": "random",
            **ds.optimize_via_rl(n_episodes=n_episodes),
        }

    results = []
    for _ in range(n_episodes):
        ep = run_ppo_episode(model)
        results.append(ep)

    best = max(results, key=lambda r: r.get("total_reward", 0))
    avg_reward = sum(r.get("total_reward", 0) for r in results) / len(results)

    return {
        "method": "ppo",
        "episodes": n_episodes,
        "avg_reward": round(avg_reward, 3),
        "best_reward": best.get("total_reward", 0),
        "best_sequence": best.get("sequence", []),
        "best_final_drives": best.get("final_drives", {}),
    }
