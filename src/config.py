from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration loaded from environment / .env file."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # Claude Code CLI
    claude_cli_path: str = "claude"  # assumes `claude` is on PATH

    # mem0 (optional)
    mem0_api_key: str = ""

    # Model routing (passed to claude CLI via --model)
    default_model: str = "sonnet"
    manager_model: str = "sonnet"
    heavy_model: str = "opus"

    # OpenAI / ChatGPT (used for Manager agent when configured)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"  # gpt-4o, gpt-4-turbo, gpt-4o-mini, etc.
    manager_backend: str = "auto"  # "auto" | "openai" | "claude"
    # auto = use OpenAI if key present, else Claude CLI

    # Daily call limit (not cost — just a rate guard)
    daily_call_limit: int = 200

    # Rate limit estimation (Pro plan defaults)
    # Session = 5-hour rolling window, Weekly = 7-day rolling window
    # These are estimated caps — Anthropic doesn't publish exact numbers
    session_limit_tokens: int = 15_000_000  # ~45M tokens per 5hr session
    weekly_limit_tokens: int = 150_000_000  # ~500M tokens per 7 days
    session_window_hours: int = 5
    weekly_window_days: int = 7

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Runner connector (control plane → local runner via reverse SSH tunnel)
    runner_base_url: str = "http://127.0.0.1:17777"
    runner_token: str = ""
    runner_poll_interval: int = 10  # seconds between health polls

    # Logging
    log_level: str = "INFO"

    # Notifications (optional — Slack / Telegram / Discord)
    slack_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""     # Discord webhook for push notifications
    discord_bot_token: str = ""       # Discord bot token for bidirectional comms
    discord_channel_id: str = ""      # Discord channel ID the bot listens in


settings = Settings()
