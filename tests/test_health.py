"""Tests for the Health Check Engine + SQLite Store."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.health.engine import (
    CheckResult,
    HealthStore,
    Status,
    execute_check,
    run_dns_check,
    run_http_check,
    run_tcp_check,
    run_tls_check,
)
from src.projects.registry import HealthCheckDef


# ── CheckResult ──────────────────────────────────────────────────────────────


class TestCheckResult:
    def test_auto_timestamp(self) -> None:
        r = CheckResult(
            project_id="p1", check_id="c1", check_type="http",
            status=Status.UP, latency_ms=42.0,
        )
        assert r.timestamp  # auto-set
        assert "T" in r.timestamp

    def test_explicit_timestamp(self) -> None:
        r = CheckResult(
            project_id="p1", check_id="c1", check_type="http",
            status=Status.DOWN, latency_ms=0, timestamp="2025-01-01T00:00:00Z",
        )
        assert r.timestamp == "2025-01-01T00:00:00Z"


# ── HTTP check ───────────────────────────────────────────────────────────────


class TestHTTPCheck:
    @patch("src.health.engine.httpx.Client")
    def test_success(self, mock_client_cls) -> None:
        mock_resp = type("Resp", (), {"status_code": 200, "json": lambda self: {"status": "ok"}})()
        mock_client = mock_client_cls.return_value.__enter__ = lambda s: s
        mock_client_cls.return_value.__exit__ = lambda s, *a: None
        mock_client_cls.return_value.request = lambda *a, **kw: mock_resp

        result = run_http_check("http://localhost/health", timeout_ms=3000)
        assert result.status == Status.UP
        assert result.latency_ms >= 0

    def test_invalid_url(self) -> None:
        result = run_http_check("http://256.256.256.256:99999/nope", timeout_ms=1000)
        assert result.status == Status.DOWN


# ── DNS check ────────────────────────────────────────────────────────────────


class TestDNSCheck:
    def test_localhost_resolves(self) -> None:
        result = run_dns_check("localhost", timeout_ms=5000)
        assert result.status == Status.UP
        assert result.latency_ms >= 0

    def test_invalid_hostname(self) -> None:
        result = run_dns_check("this-host-does-not-exist-xyz.invalid", timeout_ms=2000)
        assert result.status == Status.DOWN


# ── execute_check dispatcher ─────────────────────────────────────────────────


class TestExecuteCheck:
    def test_unknown_type(self) -> None:
        check = HealthCheckDef(id="c1", type="foobar")
        result = execute_check(check, "proj1")
        assert result.status == Status.UNKNOWN
        assert result.project_id == "proj1"
        assert result.check_id == "c1"

    def test_tags_result(self) -> None:
        check = HealthCheckDef(id="dns-check", type="dns", hostname="localhost")
        result = execute_check(check, "my-project")
        assert result.project_id == "my-project"
        assert result.check_id == "dns-check"


# ── HealthStore (SQLite) ─────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> HealthStore:
    return HealthStore(db_path=tmp_path / "test_health.db")


class TestHealthStore:
    def test_store_and_get_latest(self, store: HealthStore) -> None:
        r = CheckResult(
            project_id="p1", check_id="c1", check_type="http",
            status=Status.UP, latency_ms=42.0, message="OK",
        )
        store.store_result(r)
        latest = store.get_latest("p1", "c1")
        assert latest is not None
        assert latest["status"] == "up"
        assert latest["latency_ms"] == 42.0

    def test_store_multiple_get_latest(self, store: HealthStore) -> None:
        for i in range(5):
            r = CheckResult(
                project_id="p1", check_id="c1", check_type="http",
                status=Status.UP, latency_ms=float(i * 10),
                timestamp=f"2025-01-01T00:0{i}:00Z",
            )
            store.store_result(r)

        latest = store.get_latest("p1", "c1")
        assert latest is not None
        assert latest["latency_ms"] == 40.0  # last one

    def test_get_project_status(self, store: HealthStore) -> None:
        store.store_result(CheckResult(
            project_id="p1", check_id="c1", check_type="http",
            status=Status.UP, latency_ms=10, timestamp="2025-01-01T00:01:00Z",
        ))
        store.store_result(CheckResult(
            project_id="p1", check_id="c2", check_type="tls",
            status=Status.DEGRADED, latency_ms=20, timestamp="2025-01-01T00:02:00Z",
        ))

        status = store.get_project_status("p1")
        assert len(status) == 2
        ids = {s["check_id"] for s in status}
        assert ids == {"c1", "c2"}

    def test_get_all_latest(self, store: HealthStore) -> None:
        store.store_result(CheckResult(
            project_id="p1", check_id="c1", check_type="http",
            status=Status.UP, latency_ms=10,
        ))
        store.store_result(CheckResult(
            project_id="p2", check_id="c1", check_type="http",
            status=Status.DOWN, latency_ms=0,
        ))

        all_latest = store.get_all_latest()
        assert "p1" in all_latest
        assert "p2" in all_latest
        assert all_latest["p1"][0]["status"] == "up"
        assert all_latest["p2"][0]["status"] == "down"

    def test_incident_on_status_change(self, store: HealthStore) -> None:
        # First: UP
        store.store_result(CheckResult(
            project_id="p1", check_id="c1", check_type="http",
            status=Status.UP, latency_ms=10, timestamp="2025-01-01T00:00:00Z",
        ))
        # Then: DOWN → incident created
        store.store_result(CheckResult(
            project_id="p1", check_id="c1", check_type="http",
            status=Status.DOWN, latency_ms=0, timestamp="2025-01-01T00:01:00Z",
            message="Connection refused",
        ))

        incidents = store.get_open_incidents()
        assert len(incidents) == 1
        assert incidents[0]["from_status"] == "up"
        assert incidents[0]["to_status"] == "down"
        assert incidents[0]["ended_at"] is None

    def test_incident_resolved(self, store: HealthStore) -> None:
        store.store_result(CheckResult(
            project_id="p1", check_id="c1", check_type="http",
            status=Status.UP, latency_ms=10, timestamp="2025-01-01T00:00:00Z",
        ))
        store.store_result(CheckResult(
            project_id="p1", check_id="c1", check_type="http",
            status=Status.DOWN, latency_ms=0, timestamp="2025-01-01T00:01:00Z",
        ))
        store.store_result(CheckResult(
            project_id="p1", check_id="c1", check_type="http",
            status=Status.UP, latency_ms=15, timestamp="2025-01-01T00:02:00Z",
        ))

        open_incidents = store.get_open_incidents()
        assert len(open_incidents) == 0

        all_incidents = store.get_incidents("p1")
        assert len(all_incidents) == 1
        assert all_incidents[0]["ended_at"] is not None

    def test_get_history(self, store: HealthStore) -> None:
        for i in range(10):
            store.store_result(CheckResult(
                project_id="p1", check_id="c1", check_type="http",
                status=Status.UP, latency_ms=float(i),
                timestamp=f"2025-01-01T00:{i:02d}:00Z",
            ))

        history = store.get_history("p1", "c1", limit=5)
        assert len(history) == 5
        # Should be in DESC order
        assert history[0]["latency_ms"] == 9.0

    def test_uptime_24h_no_data(self, store: HealthStore) -> None:
        uptime = store.get_uptime_24h("p1", "c1")
        assert uptime == 100.0  # no data = assume up

    def test_cleanup_old(self, store: HealthStore) -> None:
        store.store_result(CheckResult(
            project_id="p1", check_id="c1", check_type="http",
            status=Status.UP, latency_ms=10,
            timestamp="2020-01-01T00:00:00Z",  # very old
        ))
        store.store_result(CheckResult(
            project_id="p1", check_id="c1", check_type="http",
            status=Status.UP, latency_ms=10,
        ))

        removed = store.cleanup_old(days=30)
        assert removed == 1

        history = store.get_history("p1", "c1")
        assert len(history) == 1

    def test_close(self, store: HealthStore) -> None:
        store.close()
        # Can re-open
        store.store_result(CheckResult(
            project_id="p1", check_id="c1", check_type="http",
            status=Status.UP, latency_ms=10,
        ))
        latest = store.get_latest("p1", "c1")
        assert latest is not None
