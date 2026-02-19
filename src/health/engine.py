"""Health check engine — runs checks and stores results in SQLite.

Supports: HTTP(S), TLS cert expiry, DNS resolve, TCP connect.
Each check produces a CheckResult stored in the DB with full time series.
"""

from __future__ import annotations

import json
import logging
import socket
import ssl
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent.parent / "data" / "health.db"


# ── Models ───────────────────────────────────────────────────────────────────


class Status(str, Enum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    """Result of a single health check execution."""

    project_id: str
    check_id: str
    check_type: str
    status: Status
    latency_ms: float
    status_code: int | None = None
    message: str = ""
    details: dict[str, Any] | None = None
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


# ── Check runners ────────────────────────────────────────────────────────────


def run_http_check(
    url: str,
    method: str = "GET",
    expected_status: int = 200,
    timeout_ms: int = 10_000,
) -> CheckResult:
    """HTTP(S) health check — GET/HEAD with status code + latency."""
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_ms / 1000, follow_redirects=True, verify=True) as client:
            resp = client.request(method, url)
        latency = (time.perf_counter() - t0) * 1000

        if resp.status_code == expected_status:
            status = Status.UP
            # Check latency budget — degrade if > 3s
            if latency > 3000:
                status = Status.DEGRADED
            msg = f"{resp.status_code} OK"
        else:
            status = Status.DOWN
            msg = f"Expected {expected_status}, got {resp.status_code}"

        # Try to parse JSON health body
        details = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                details = {k: body[k] for k in ("status", "version", "commit", "deps") if k in body}
        except Exception:
            pass

        return CheckResult(
            project_id="", check_id="", check_type="http",
            status=status, latency_ms=round(latency, 1),
            status_code=resp.status_code, message=msg, details=details,
        )
    except httpx.ConnectTimeout:
        return CheckResult(
            project_id="", check_id="", check_type="http",
            status=Status.DOWN, latency_ms=timeout_ms,
            message=f"Connection timed out ({timeout_ms}ms)",
        )
    except httpx.ConnectError as e:
        latency = (time.perf_counter() - t0) * 1000
        return CheckResult(
            project_id="", check_id="", check_type="http",
            status=Status.DOWN, latency_ms=round(latency, 1),
            message=f"Connection error: {e}",
        )
    except Exception as e:
        latency = (time.perf_counter() - t0) * 1000
        return CheckResult(
            project_id="", check_id="", check_type="http",
            status=Status.DOWN, latency_ms=round(latency, 1),
            message=f"Error: {type(e).__name__}: {e}",
        )


def run_tls_check(
    hostname: str,
    port: int = 443,
    warn_days_before: int = 14,
    timeout_ms: int = 10_000,
) -> CheckResult:
    """Check TLS certificate expiry."""
    t0 = time.perf_counter()
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=timeout_ms / 1000) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()

        latency = (time.perf_counter() - t0) * 1000

        if not cert:
            return CheckResult(
                project_id="", check_id="", check_type="tls",
                status=Status.DOWN, latency_ms=round(latency, 1),
                message="No certificate returned",
            )

        # Parse expiry
        not_after = cert.get("notAfter", "")
        expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days_left = (expiry - now).days

        if days_left < 0:
            status = Status.DOWN
            msg = f"Certificate EXPIRED {-days_left} days ago"
        elif days_left < warn_days_before:
            status = Status.DEGRADED
            msg = f"Certificate expires in {days_left} days (warn < {warn_days_before})"
        else:
            status = Status.UP
            msg = f"Certificate valid, expires in {days_left} days"

        return CheckResult(
            project_id="", check_id="", check_type="tls",
            status=status, latency_ms=round(latency, 1),
            message=msg, details={"days_left": days_left, "expiry": expiry.isoformat()},
        )
    except Exception as e:
        latency = (time.perf_counter() - t0) * 1000
        return CheckResult(
            project_id="", check_id="", check_type="tls",
            status=Status.DOWN, latency_ms=round(latency, 1),
            message=f"TLS error: {type(e).__name__}: {e}",
        )


def run_dns_check(
    hostname: str,
    timeout_ms: int = 5_000,
) -> CheckResult:
    """DNS resolution check."""
    t0 = time.perf_counter()
    try:
        socket.setdefaulttimeout(timeout_ms / 1000)
        addrs = socket.getaddrinfo(hostname, None)
        latency = (time.perf_counter() - t0) * 1000

        ips = sorted({a[4][0] for a in addrs})
        return CheckResult(
            project_id="", check_id="", check_type="dns",
            status=Status.UP, latency_ms=round(latency, 1),
            message=f"Resolved to {', '.join(ips[:3])}",
            details={"ips": ips},
        )
    except socket.gaierror as e:
        latency = (time.perf_counter() - t0) * 1000
        return CheckResult(
            project_id="", check_id="", check_type="dns",
            status=Status.DOWN, latency_ms=round(latency, 1),
            message=f"DNS resolution failed: {e}",
        )
    except Exception as e:
        latency = (time.perf_counter() - t0) * 1000
        return CheckResult(
            project_id="", check_id="", check_type="dns",
            status=Status.DOWN, latency_ms=round(latency, 1),
            message=f"DNS error: {type(e).__name__}: {e}",
        )
    finally:
        socket.setdefaulttimeout(None)


def run_tcp_check(
    hostname: str,
    port: int = 443,
    timeout_ms: int = 5_000,
) -> CheckResult:
    """Raw TCP port connectivity check."""
    t0 = time.perf_counter()
    try:
        sock = socket.create_connection((hostname, port), timeout=timeout_ms / 1000)
        sock.close()
        latency = (time.perf_counter() - t0) * 1000
        return CheckResult(
            project_id="", check_id="", check_type="tcp",
            status=Status.UP, latency_ms=round(latency, 1),
            message=f"Port {port} open",
        )
    except Exception as e:
        latency = (time.perf_counter() - t0) * 1000
        return CheckResult(
            project_id="", check_id="", check_type="tcp",
            status=Status.DOWN, latency_ms=round(latency, 1),
            message=f"TCP connect failed: {type(e).__name__}: {e}",
        )


# Dispatcher
CHECK_RUNNERS = {
    "http": lambda c: run_http_check(c.url, c.method, c.expected_status, c.timeout_ms),
    "tls": lambda c: run_tls_check(c.hostname, 443, c.warn_days_before, c.timeout_ms),
    "dns": lambda c: run_dns_check(c.hostname, c.timeout_ms),
    "tcp": lambda c: run_tcp_check(c.hostname, 443, c.timeout_ms),
}


def execute_check(check_def: Any, project_id: str) -> CheckResult:
    """Run a health check by type and tag the result with project/check IDs."""
    runner = CHECK_RUNNERS.get(check_def.type)
    if not runner:
        return CheckResult(
            project_id=project_id, check_id=check_def.id, check_type=check_def.type,
            status=Status.UNKNOWN, latency_ms=0,
            message=f"Unknown check type: {check_def.type}",
        )
    result = runner(check_def)
    result.project_id = project_id
    result.check_id = check_def.id
    return result


# ── SQLite storage ───────────────────────────────────────────────────────────


class HealthStore:
    """SQLite-backed storage for health check results + incidents."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS check_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                check_id TEXT NOT NULL,
                check_type TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms REAL,
                status_code INTEGER,
                message TEXT,
                details TEXT,
                timestamp TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_results_project
                ON check_results (project_id, check_id, timestamp DESC);

            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                check_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                from_status TEXT NOT NULL,
                to_status TEXT NOT NULL,
                message TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_incidents_project
                ON incidents (project_id, started_at DESC);
        """)
        conn.commit()

    def store_result(self, result: CheckResult) -> None:
        """Insert a check result and detect status transitions (incidents)."""
        conn = self._get_conn()

        # Get previous status
        prev_row = conn.execute(
            "SELECT status FROM check_results "
            "WHERE project_id = ? AND check_id = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (result.project_id, result.check_id),
        ).fetchone()

        prev_status = prev_row["status"] if prev_row else None

        # Insert result
        conn.execute(
            "INSERT INTO check_results "
            "(project_id, check_id, check_type, status, latency_ms, status_code, message, details, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.project_id, result.check_id, result.check_type,
                result.status.value, result.latency_ms, result.status_code,
                result.message, json.dumps(result.details) if result.details else None,
                result.timestamp,
            ),
        )

        # Detect status change → create/close incident
        if prev_status and prev_status != result.status.value:
            if result.status in (Status.DOWN, Status.DEGRADED) and prev_status == Status.UP.value:
                # New incident
                conn.execute(
                    "INSERT INTO incidents (project_id, check_id, started_at, from_status, to_status, message) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (result.project_id, result.check_id, result.timestamp,
                     prev_status, result.status.value, result.message),
                )
            elif result.status == Status.UP and prev_status in (Status.DOWN.value, Status.DEGRADED.value):
                # Close open incident
                conn.execute(
                    "UPDATE incidents SET ended_at = ? "
                    "WHERE project_id = ? AND check_id = ? AND ended_at IS NULL",
                    (result.timestamp, result.project_id, result.check_id),
                )

        conn.commit()

    def get_latest(self, project_id: str, check_id: str) -> dict[str, Any] | None:
        """Get the most recent result for a specific check."""
        row = self._get_conn().execute(
            "SELECT * FROM check_results "
            "WHERE project_id = ? AND check_id = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (project_id, check_id),
        ).fetchone()
        return dict(row) if row else None

    def get_project_status(self, project_id: str) -> list[dict[str, Any]]:
        """Get latest result for each check of a project."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT cr.* FROM check_results cr "
            "INNER JOIN ("
            "  SELECT project_id, check_id, MAX(timestamp) as max_ts "
            "  FROM check_results WHERE project_id = ? "
            "  GROUP BY project_id, check_id"
            ") latest ON cr.project_id = latest.project_id "
            "AND cr.check_id = latest.check_id "
            "AND cr.timestamp = latest.max_ts",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_latest(self) -> dict[str, list[dict[str, Any]]]:
        """Get latest results grouped by project_id."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT cr.* FROM check_results cr "
            "INNER JOIN ("
            "  SELECT project_id, check_id, MAX(timestamp) as max_ts "
            "  FROM check_results "
            "  GROUP BY project_id, check_id"
            ") latest ON cr.project_id = latest.project_id "
            "AND cr.check_id = latest.check_id "
            "AND cr.timestamp = latest.max_ts "
            "ORDER BY cr.project_id",
        ).fetchall()

        result: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            d = dict(r)
            result.setdefault(d["project_id"], []).append(d)
        return result

    def get_history(
        self, project_id: str, check_id: str, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get time series of results for a check."""
        rows = self._get_conn().execute(
            "SELECT * FROM check_results "
            "WHERE project_id = ? AND check_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (project_id, check_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_open_incidents(self) -> list[dict[str, Any]]:
        """Get all incidents that haven't been resolved."""
        rows = self._get_conn().execute(
            "SELECT * FROM incidents WHERE ended_at IS NULL ORDER BY started_at DESC",
        ).fetchall()
        return [dict(r) for r in rows]

    def get_incidents(self, project_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent incidents, optionally filtered by project."""
        if project_id:
            rows = self._get_conn().execute(
                "SELECT * FROM incidents WHERE project_id = ? ORDER BY started_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        else:
            rows = self._get_conn().execute(
                "SELECT * FROM incidents ORDER BY started_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_uptime_24h(self, project_id: str, check_id: str) -> float:
        """Calculate uptime percentage over the last 24 hours."""
        cutoff = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        # Simplified: use 24h ago as cutoff (ISO sort works for UTC)
        from datetime import timedelta
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=24)
        cutoff = cutoff_dt.isoformat()

        rows = self._get_conn().execute(
            "SELECT status FROM check_results "
            "WHERE project_id = ? AND check_id = ? AND timestamp >= ? "
            "ORDER BY timestamp",
            (project_id, check_id, cutoff),
        ).fetchall()

        if not rows:
            return 100.0  # No data = assume up

        up_count = sum(1 for r in rows if r["status"] == Status.UP.value)
        return round(up_count / len(rows) * 100, 1)

    def cleanup_old(self, days: int = 30) -> int:
        """Remove check results older than N days."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM check_results WHERE timestamp < ?", (cutoff,),
        )
        conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
