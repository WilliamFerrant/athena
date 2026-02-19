"""Tests for task board storage."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.tasks.store import COLUMNS, Task, TaskStore


@pytest.fixture
def store(tmp_path):
    """TaskStore backed by a temp SQLite file."""
    return TaskStore(db_path=tmp_path / "test_tasks.db")


class TestTask:
    def test_defaults(self):
        t = Task()
        assert t.column == "backlog"
        assert t.priority == 0
        assert t.autopilot is False
        assert t.id  # not empty
        assert t.created_at > 0

    def test_to_dict(self):
        t = Task(title="Write tests", description="pytest", metadata={"key": "val"})
        d = t.to_dict()
        assert d["title"] == "Write tests"
        assert '"key"' in d["metadata"]  # JSON string

    def test_from_row(self):
        row = {
            "id": "abc123",
            "title": "Task",
            "description": "Desc",
            "column": "planned",
            "project_id": "proj1",
            "agent": "backend",
            "priority": 1,
            "autopilot": 1,
            "created_at": 100.0,
            "updated_at": 200.0,
            "result": "OK",
            "metadata": '{"foo":"bar"}',
        }
        t = Task.from_row(row)
        assert t.id == "abc123"
        assert t.column == "planned"
        assert t.autopilot is True
        assert t.metadata == {"foo": "bar"}


class TestTaskStore:
    def test_create_and_get(self, store):
        task = store.create(Task(title="Test task"))
        assert task.id
        fetched = store.get(task.id)
        assert fetched is not None
        assert fetched.title == "Test task"
        assert fetched.column == "backlog"

    def test_list_all(self, store):
        store.create(Task(title="A"))
        store.create(Task(title="B"))
        all_tasks = store.list_all()
        assert len(all_tasks) == 2

    def test_list_by_project(self, store):
        store.create(Task(title="A", project_id="proj1"))
        store.create(Task(title="B", project_id="proj2"))
        store.create(Task(title="C", project_id="proj1"))
        assert len(store.list_all("proj1")) == 2
        assert len(store.list_all("proj2")) == 1

    def test_list_by_column(self, store):
        store.create(Task(title="A", column="backlog"))
        store.create(Task(title="B", column="in-progress"))
        store.create(Task(title="C", column="backlog"))
        assert len(store.list_by_column("backlog")) == 2
        assert len(store.list_by_column("in-progress")) == 1
        assert len(store.list_by_column("done")) == 0

    def test_update(self, store):
        task = store.create(Task(title="Old"))
        updated = store.update(task.id, title="New", priority=2)
        assert updated is not None
        assert updated.title == "New"
        assert updated.priority == 2

    def test_update_nonexistent(self, store):
        assert store.update("fakeid") is None

    def test_move(self, store):
        task = store.create(Task(title="Move me"))
        assert task.column == "backlog"
        moved = store.move(task.id, "in-progress")
        assert moved is not None
        assert moved.column == "in-progress"

    def test_move_invalid_column(self, store):
        task = store.create(Task(title="X"))
        with pytest.raises(ValueError, match="Invalid column"):
            store.move(task.id, "invalid")

    def test_delete(self, store):
        task = store.create(Task(title="Del me"))
        assert store.delete(task.id) is True
        assert store.get(task.id) is None

    def test_delete_nonexistent(self, store):
        assert store.delete("fakeid") is False

    def test_board(self, store):
        store.create(Task(title="A", column="backlog"))
        store.create(Task(title="B", column="in-progress"))
        store.create(Task(title="C", column="done"))
        board = store.board()
        assert len(board["backlog"]) == 1
        assert len(board["in-progress"]) == 1
        assert len(board["done"]) == 1
        assert len(board["planned"]) == 0
        assert len(board["review"]) == 0

    def test_board_by_project(self, store):
        store.create(Task(title="A", column="backlog", project_id="p1"))
        store.create(Task(title="B", column="backlog", project_id="p2"))
        board = store.board("p1")
        assert len(board["backlog"]) == 1
        assert board["backlog"][0]["title"] == "A"

    def test_stats(self, store):
        store.create(Task(title="A", column="backlog"))
        store.create(Task(title="B", column="in-progress", autopilot=True))
        store.create(Task(title="C", column="done"))
        stats = store.stats()
        assert stats["total"] == 3
        assert stats["by_column"]["backlog"] == 1
        assert stats["by_column"]["in-progress"] == 1
        assert stats["autopilot_count"] == 1

    def test_priority_ordering(self, store):
        store.create(Task(title="Low", priority=0, column="backlog"))
        store.create(Task(title="High", priority=1, column="backlog"))
        store.create(Task(title="Urgent", priority=2, column="backlog"))
        tasks = store.list_by_column("backlog")
        # Higher priority first
        assert tasks[0].title == "Urgent"
        assert tasks[1].title == "High"

    def test_autopilot_toggle(self, store):
        task = store.create(Task(title="Auto", autopilot=False))
        assert not task.autopilot
        updated = store.update(task.id, autopilot=True)
        assert updated.autopilot is True

    def test_columns_constant(self):
        assert COLUMNS == ("backlog", "planned", "in-progress", "review", "done")
