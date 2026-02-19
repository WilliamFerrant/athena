"""Entry point for the CLA runner — `companion-runner` console script."""

from __future__ import annotations

import logging
import sys

import uvicorn
from rich.console import Console
from rich.panel import Panel

from src.runner.config import runner_settings

console = Console()


def main() -> None:
    """Start the local runner service."""
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, runner_settings.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    token_status = "SET" if runner_settings.runner_token else "NOT SET (dev mode — no auth)"

    console.print(
        Panel.fit(
            f"[bold]CLA Local Runner[/bold]\n"
            f"Bind:  {runner_settings.runner_host}:{runner_settings.runner_port}\n"
            f"Token: {token_status}\n"
            f"Projects: {runner_settings.runner_projects_file}\n"
            f"Platform: {sys.platform}",
            title="companion-runner",
            border_style="green",
        )
    )

    if not runner_settings.runner_token:
        console.print(
            "[yellow]WARNING: No RUNNER_TOKEN set. "
            "All requests will be accepted without auth. "
            "Set RUNNER_TOKEN in .env for production use.[/yellow]\n"
        )

    uvicorn.run(
        "src.runner.app:runner_app",
        host=runner_settings.runner_host,
        port=runner_settings.runner_port,
        log_level=runner_settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
