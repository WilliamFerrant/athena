"""Runner configuration — loaded from environment / .env file."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class RunnerSettings(BaseSettings):
    """Settings specific to the local runner."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # Bind address
    runner_host: str = "127.0.0.1"
    runner_port: int = 7776

    # Auth
    runner_token: str = ""

    # Claude CLI
    claude_cli_path: str = "claude"

    # Projects file (absolute or relative to CWD)
    runner_projects_file: str = "projects.yaml"

    # Safety
    runner_max_diff_bytes: int = 500_000  # 500 KB max diff payload
    runner_command_timeout: int = 1200  # 20 min default
    runner_claude_timeout: int = 7200  # 2 hr default

    # Logging
    log_level: str = "INFO"


runner_settings = RunnerSettings()
