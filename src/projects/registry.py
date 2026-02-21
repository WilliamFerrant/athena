"""Project registry — loads projects.yaml and provides typed models.

Single source of truth for all project metadata.
The dashboard, orchestrator, and health engine all consume this.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

REGISTRY_PATH = Path(__file__).parent.parent.parent / "projects.yaml"


# ── Data models ──────────────────────────────────────────────────────────────


@dataclass
class HealthCheckDef:
    """Definition of a single health check from the registry."""

    id: str
    type: str  # http | tls | dns | tcp | command
    url: str = ""
    hostname: str = ""
    method: str = "GET"
    expected_status: int = 200
    timeout_ms: int = 10_000
    interval_seconds: int = 60
    warn_days_before: int = 14  # for TLS checks
    command: str = ""  # for command checks


@dataclass
class EnvConfig:
    """URLs for a single environment (local / staging / prod)."""

    name: str
    app_url: str = ""
    api_url: str = ""
    admin_url: str = ""


@dataclass
class Runbook:
    """Quick-reference command or doc link."""

    label: str
    command: str = ""
    url: str = ""


@dataclass
class LocalConfig:
    """Local development paths per platform."""

    path_windows: str = ""
    path_linux: str = ""
    path_mac: str = ""


@dataclass
class ProjectCommands:
    """Named commands for a project (dev/test/lint/build etc.)."""

    dev: str = ""
    test: str = ""
    lint: str = ""
    build: str = ""
    start: str = ""


@dataclass
class Project:
    """A registered project with all its metadata."""

    id: str
    name: str
    group: str = "internal"  # active-clients | internal | paused | r-and-d
    priority: str = "medium"  # high | medium | low
    ownership: str = "personal"

    repo_path: str = ""
    git_remote: str = ""

    local: LocalConfig = field(default_factory=LocalConfig)
    commands: ProjectCommands = field(default_factory=ProjectCommands)

    envs: list[EnvConfig] = field(default_factory=list)
    health_checks: list[HealthCheckDef] = field(default_factory=list)
    runbooks: list[Runbook] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


# ── Registry ─────────────────────────────────────────────────────────────────


class ProjectRegistry:
    """Loads and caches projects from projects.yaml."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or REGISTRY_PATH
        self._projects: list[Project] = []
        self._loaded = False

    def load(self, force: bool = False) -> list[Project]:
        """Parse projects.yaml and return Project list."""
        if self._loaded and not force:
            return self._projects

        self._projects = []
        if not self._path.exists():
            logger.warning("Registry file not found: %s", self._path)
            self._loaded = True
            return self._projects

        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("Failed to parse %s: %s", self._path, e)
            self._loaded = True
            return self._projects

        for entry in raw.get("projects", []) or []:
            try:
                self._projects.append(_parse_project(entry))
            except Exception as e:
                logger.warning("Skipping malformed project entry: %s", e)

        self._loaded = True
        logger.info("Loaded %d projects from registry", len(self._projects))
        return self._projects

    @property
    def projects(self) -> list[Project]:
        return self.load()

    def get(self, project_id: str) -> Project | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def by_group(self) -> dict[str, list[Project]]:
        groups: dict[str, list[Project]] = {}
        for p in self.projects:
            groups.setdefault(p.group, []).append(p)
        return groups

    def all_health_checks(self) -> list[tuple[Project, HealthCheckDef]]:
        """Return all (project, check) pairs for the health scheduler."""
        result = []
        for p in self.projects:
            for c in p.health_checks:
                result.append((p, c))
        return result

    def to_dict(self) -> list[dict[str, Any]]:
        """Serialize all projects for the API."""
        return [_project_to_dict(p) for p in self.projects]

    def reload(self) -> list[Project]:
        """Force reload from disk."""
        return self.load(force=True)

    def add_project(self, data: dict[str, Any]) -> Project:
        """Append a new project entry to projects.yaml and return the parsed Project.

        ``data`` must contain at least ``id`` and ``name``.
        Raises ``ValueError`` if the project id already exists.
        """
        project_id = data.get("id", "").strip()
        if not project_id:
            raise ValueError("Project 'id' is required")
        if self.get(project_id):
            raise ValueError(f"Project '{project_id}' already exists in registry")

        # Parse into a typed Project (validates structure)
        project = _parse_project(data)

        # Read current YAML, append, write back
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            raise RuntimeError(f"Could not read registry file: {e}") from e

        projects_list: list[dict] = raw.get("projects") or []
        projects_list.append(data)
        raw["projects"] = projects_list

        self._path.write_text(
            yaml.dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )

        # Update in-memory cache
        self._projects.append(project)
        logger.info("Added project '%s' to registry", project_id)
        return project


# ── Parsers ──────────────────────────────────────────────────────────────────


def _parse_project(raw: dict[str, Any]) -> Project:
    envs = []
    raw_envs = raw.get("envs") or []
    if isinstance(raw_envs, dict):
        # Dict format: { local: {app_url: ...}, prod: {app_url: ...} }
        for env_name, env_data in raw_envs.items():
            envs.append(
                EnvConfig(
                    name=env_name,
                    app_url=env_data.get("app_url", ""),
                    api_url=env_data.get("api_url", ""),
                    admin_url=env_data.get("admin_url", ""),
                )
            )
    else:
        # List format: [{ name: local, app_url: ... }, ...]
        for e in raw_envs:
            envs.append(
                EnvConfig(
                    name=e.get("name", "unknown"),
                    app_url=e.get("app_url", ""),
                    api_url=e.get("api_url", ""),
                    admin_url=e.get("admin_url", ""),
                )
            )

    checks = []
    for c in raw.get("health_checks") or []:
        checks.append(
            HealthCheckDef(
                id=c.get("id", "unnamed"),
                type=c.get("type", "http"),
                url=c.get("url", ""),
                hostname=c.get("hostname", ""),
                method=c.get("method", "GET"),
                expected_status=c.get("expected_status", 200),
                timeout_ms=c.get("timeout_ms", 10_000),
                interval_seconds=c.get("interval_seconds", 60),
                warn_days_before=c.get("warn_days_before", 14),
                command=c.get("command", ""),
            )
        )

    runbooks = []
    for r in raw.get("runbooks") or []:
        runbooks.append(
            Runbook(
                label=r.get("label", ""),
                command=r.get("command", ""),
                url=r.get("url", ""),
            )
        )

    # Parse local config
    raw_local = raw.get("local") or {}
    local_cfg = LocalConfig(
        path_windows=raw_local.get("path_windows", ""),
        path_linux=raw_local.get("path_linux", ""),
        path_mac=raw_local.get("path_mac", ""),
    )

    # Parse commands
    raw_cmds = raw.get("commands") or {}
    commands = ProjectCommands(
        dev=raw_cmds.get("dev", ""),
        test=raw_cmds.get("test", ""),
        lint=raw_cmds.get("lint", ""),
        build=raw_cmds.get("build", ""),
        start=raw_cmds.get("start", ""),
    )

    return Project(
        id=raw["id"],
        name=raw.get("name", raw["id"]),
        group=raw.get("group", "internal"),
        priority=raw.get("priority", "medium"),
        ownership=raw.get("ownership", "personal"),
        repo_path=raw.get("repo_path", ""),
        git_remote=raw.get("git_remote", ""),
        local=local_cfg,
        commands=commands,
        envs=envs,
        health_checks=checks,
        runbooks=runbooks,
        tags=raw.get("tags") or [],
    )


def _project_to_dict(p: Project) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "group": p.group,
        "priority": p.priority,
        "ownership": p.ownership,
        "repo_path": p.repo_path,
        "git_remote": p.git_remote,
        "local": {
            "path_windows": p.local.path_windows,
            "path_linux": p.local.path_linux,
            "path_mac": p.local.path_mac,
        },
        "commands": {
            "dev": p.commands.dev,
            "test": p.commands.test,
            "lint": p.commands.lint,
            "build": p.commands.build,
            "start": p.commands.start,
        },
        "envs": [
            {"name": e.name, "app_url": e.app_url, "api_url": e.api_url, "admin_url": e.admin_url}
            for e in p.envs
        ],
        "health_checks": [
            {
                "id": c.id, "type": c.type, "url": c.url, "hostname": c.hostname,
                "interval_seconds": c.interval_seconds,
            }
            for c in p.health_checks
        ],
        "runbooks": [
            {"label": r.label, "command": r.command, "url": r.url}
            for r in p.runbooks
        ],
        "tags": p.tags,
    }
