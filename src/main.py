"""Entry point for the Athena multi-agent system."""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn
from rich.console import Console
from rich.panel import Panel

from src.config import settings
from src.orchestrator.graph import run_task
from src.token_tracker.tracker import TokenTracker

console = Console()
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


def run_server() -> None:
    """Start the FastAPI server."""
    console.print(Panel("Starting Athena API Server", style="bold green"))
    uvicorn.run(
        "src.api.server:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )


def run_cli(task: str) -> None:
    """Run a single task through the multi-agent pipeline via CLI."""
    console.print(Panel(f"Task: {task}", title="Athena", style="bold blue"))

    tracker = TokenTracker()

    with console.status("[bold green]Agents working..."):
        result = run_task(task, tracker=tracker)

    # Display results
    console.print("\n[bold]Plan:[/bold]")
    console.print(result.get("plan", "No plan generated"))

    console.print("\n[bold]Final Output:[/bold]")
    console.print(result.get("final_output", "No output generated"))

    # Token summary
    summary = tracker.global_summary()
    console.print(f"\n[dim]Tokens: {summary['total_input_tokens']}in / {summary['total_output_tokens']}out | Cost: ${summary['total_cost_usd']:.4f}[/dim]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Athena Multi-Agent System")
    sub = parser.add_subparsers(dest="command")

    # Server mode
    sub.add_parser("serve", help="Start the API server")

    # CLI mode
    cli_parser = sub.add_parser("run", help="Run a task via CLI")
    cli_parser.add_argument("task", help="The task to execute")

    args = parser.parse_args()

    if args.command == "serve":
        run_server()
    elif args.command == "run":
        run_cli(args.task)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

