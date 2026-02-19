"""Task board storage — SQLite-backed kanban for Athena.

Columns: backlog → planned → in-progress → review → done
Each task can be assigned to an agent and optionally linked to a project.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
DB_PATH = DATA_DIR / "tasks.db"

COLUMNS = ("backlog", "planned", "in-progress", "review", "done")


@dataclass
class Task:
    """A single task on the board."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = ""
    description: str = ""
    column: str = "backlog"
    project_id: str | None = None
    agent: str | None = None
    priority: int = 0  # 0=normal, 1=high, 2=urgent
    autopilot: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["metadata"] = json.dumps(d["metadata"])
        return d

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Task":
        meta = row.get("metadata", "{}")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        return cls(
            id=row["id"],
            title=row.get("title", ""),
            description=row.get("description", ""),
            column=row.get("column", "backlog"),
            project_id=row.get("project_id"),
            agent=row.get("agent"),
            priority=row.get("priority", 0),
            autopilot=bool(row.get("autopilot", 0)),
            created_at=row.get("created_at", 0.0),
            updated_at=row.get("updated_at", 0.0),
            result=row.get("result", ""),
            metadata=meta,
        )


class TaskStore:
    """SQLite-backed task board storage."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = str(db_path or DB_PATH)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id          TEXT PRIMARY KEY,
                    title       TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    "column"    TEXT NOT NULL DEFAULT 'backlog',
                    project_id  TEXT,
                    agent       TEXT,
                    priority    INTEGER NOT NULL DEFAULT 0,
                    autopilot   INTEGER NOT NULL DEFAULT 0,
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL,
                    result      TEXT NOT NULL DEFAULT '',
                    metadata    TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_tasks_column
                ON tasks ("column")
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_tasks_project
                ON tasks (project_id)
            """)

    # ── CRUD ──────────────────────────────────────────────────────────────

    def create(self, task: Task) -> Task:
        """Insert a new task."""
        task.created_at = time.time()
        task.updated_at = task.created_at
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO tasks (id, title, description, "column", project_id,
                                   agent, priority, autopilot, created_at,
                                   updated_at, result, metadata)
                VALUES (:id, :title, :description, :column, :project_id,
                        :agent, :priority, :autopilot, :created_at,
                        :updated_at, :result, :metadata)
            """, task.to_dict())
        return task

    def get(self, task_id: str) -> Task | None:
        """Get a single task by ID."""
        with self._conn() as conn:
            row = conn.execute(
                'SELECT * FROM tasks WHERE id = ?', (task_id,)
            ).fetchone()
        return Task.from_row(dict(row)) if row else None

    def list_all(self, project_id: str | None = None) -> list[Task]:
        """List all tasks, optionally filtered by project."""
        with self._conn() as conn:
            if project_id:
                rows = conn.execute(
                    'SELECT * FROM tasks WHERE project_id = ? ORDER BY priority DESC, created_at',
                    (project_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    'SELECT * FROM tasks ORDER BY priority DESC, created_at'
                ).fetchall()
        return [Task.from_row(dict(r)) for r in rows]

    def list_by_column(self, column: str, project_id: str | None = None) -> list[Task]:
        """List tasks in a specific column."""
        with self._conn() as conn:
            if project_id:
                rows = conn.execute(
                    'SELECT * FROM tasks WHERE "column" = ? AND project_id = ? '
                    'ORDER BY priority DESC, created_at',
                    (column, project_id)
                ).fetchall()
            else:
                rows = conn.execute(
                    'SELECT * FROM tasks WHERE "column" = ? '
                    'ORDER BY priority DESC, created_at',
                    (column,)
                ).fetchall()
        return [Task.from_row(dict(r)) for r in rows]

    def update(self, task_id: str, **fields: Any) -> Task | None:
        """Update specific fields of a task."""
        task = self.get(task_id)
        if not task:
            return None

        allowed = {"title", "description", "column", "project_id", "agent",
                    "priority", "autopilot", "result", "metadata"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return task

        if "metadata" in updates and isinstance(updates["metadata"], dict):
            updates["metadata"] = json.dumps(updates["metadata"])

        updates["updated_at"] = time.time()
        set_clause = ", ".join(f'"{k}" = :{k}' for k in updates)
        updates["id"] = task_id

        with self._conn() as conn:
            conn.execute(
                f'UPDATE tasks SET {set_clause} WHERE id = :id', updates
            )
        return self.get(task_id)

    def move(self, task_id: str, column: str) -> Task | None:
        """Move a task to a different column."""
        if column not in COLUMNS:
            raise ValueError(f"Invalid column: {column}. Must be one of {COLUMNS}")
        return self.update(task_id, column=column)

    def delete(self, task_id: str) -> bool:
        """Delete a task."""
        with self._conn() as conn:
            cursor = conn.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        return cursor.rowcount > 0

    def board(self, project_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Get the full board as a dict of columns → task lists."""
        tasks = self.list_all(project_id)
        board: dict[str, list[dict[str, Any]]] = {col: [] for col in COLUMNS}
        for t in tasks:
            col = t.column if t.column in COLUMNS else "backlog"
            d = asdict(t)
            d["autopilot"] = bool(d["autopilot"])
            board[col].append(d)
        return board

    def stats(self, project_id: str | None = None) -> dict[str, Any]:
        """Summary statistics for the board."""
        tasks = self.list_all(project_id)
        by_col = {col: 0 for col in COLUMNS}
        for t in tasks:
            col = t.column if t.column in COLUMNS else "backlog"
            by_col[col] += 1
        return {
            "total": len(tasks),
            "by_column": by_col,
            "autopilot_count": sum(1 for t in tasks if t.autopilot),
        }

    def close(self) -> None:
        """No-op — connections are created per-call."""
        pass
