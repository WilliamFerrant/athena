"""In-process registry for active orchestrator runs.

Each run gets a unique ID and a threading.Event that can be set to
signal the executing node to stop after the current subtask batch.
"""

from __future__ import annotations

import threading
import uuid

# run_id -> stop_event
_active_runs: dict[str, threading.Event] = {}
_lock = threading.Lock()


def start_run() -> tuple[str, threading.Event]:
    """Register a new run and return its ID and stop event."""
    run_id = uuid.uuid4().hex[:8]
    stop_event = threading.Event()
    with _lock:
        _active_runs[run_id] = stop_event
    return run_id, stop_event


def stop_run(run_id: str) -> bool:
    """Signal the run to stop. Returns True if the run was found."""
    with _lock:
        event = _active_runs.get(run_id)
    if event:
        event.set()
        return True
    return False


def end_run(run_id: str) -> None:
    """Remove a completed or stopped run from the registry."""
    with _lock:
        _active_runs.pop(run_id, None)


def get_stop_event(run_id: str) -> threading.Event | None:
    """Return the stop event for a run, or None if not found."""
    with _lock:
        return _active_runs.get(run_id)
