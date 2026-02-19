"""Tests for the Project Registry."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from src.projects.registry import (
    EnvConfig,
    HealthCheckDef,
    Project,
    ProjectRegistry,
    Runbook,
)


@pytest.fixture
def sample_yaml(tmp_path: Path) -> Path:
    """Create a minimal projects.yaml for testing."""
    data = {
        "projects": [
            {
                "id": "test-project",
                "name": "Test Project",
                "group": "internal",
                "priority": "high",
                "ownership": "me",
                "repo_path": "/tmp/test",
                "git_remote": "https://github.com/test/test.git",
                "envs": [
                    {"name": "local", "app_url": "http://localhost:3000"},
                    {"name": "prod", "app_url": "https://test.example.com", "api_url": "https://api.test.example.com"},
                ],
                "health_checks": [
                    {
                        "id": "api-health",
                        "type": "http",
                        "url": "https://api.test.example.com/health",
                        "method": "GET",
                        "expected_status": 200,
                        "timeout_ms": 5000,
                        "interval_seconds": 60,
                    },
                    {
                        "id": "tls-check",
                        "type": "tls",
                        "hostname": "test.example.com",
                        "warn_days_before": 14,
                        "timeout_ms": 5000,
                        "interval_seconds": 3600,
                    },
                ],
                "runbooks": [
                    {"label": "Deploy", "command": "make deploy"},
                    {"label": "Docs", "url": "https://docs.example.com"},
                ],
                "tags": ["python", "api"],
            },
            {
                "id": "simple-project",
                "name": "Simple",
                "group": "paused",
            },
        ]
    }
    yml_path = tmp_path / "projects.yaml"
    yml_path.write_text(yaml.dump(data))
    return yml_path


@pytest.fixture
def registry(sample_yaml: Path) -> ProjectRegistry:
    reg = ProjectRegistry(path=sample_yaml)
    reg.load()
    return reg


class TestProjectRegistry:
    def test_load_projects(self, registry: ProjectRegistry) -> None:
        projects = registry.load()
        assert len(projects) == 2
        assert projects[0].id == "test-project"
        assert projects[1].id == "simple-project"

    def test_get_project(self, registry: ProjectRegistry) -> None:
        p = registry.get("test-project")
        assert p is not None
        assert p.name == "Test Project"
        assert p.group == "internal"
        assert p.priority == "high"

    def test_get_missing(self, registry: ProjectRegistry) -> None:
        assert registry.get("nonexistent") is None

    def test_by_group(self, registry: ProjectRegistry) -> None:
        groups = registry.by_group()
        assert "internal" in groups
        assert "paused" in groups
        assert len(groups["internal"]) == 1
        assert groups["internal"][0].id == "test-project"

    def test_envs(self, registry: ProjectRegistry) -> None:
        p = registry.get("test-project")
        assert p is not None
        assert len(p.envs) == 2
        assert p.envs[0].name == "local"
        assert p.envs[1].api_url == "https://api.test.example.com"

    def test_health_checks(self, registry: ProjectRegistry) -> None:
        p = registry.get("test-project")
        assert p is not None
        assert len(p.health_checks) == 2
        http_check = p.health_checks[0]
        assert http_check.type == "http"
        assert http_check.url == "https://api.test.example.com/health"
        assert http_check.expected_status == 200

        tls_check = p.health_checks[1]
        assert tls_check.type == "tls"
        assert tls_check.hostname == "test.example.com"
        assert tls_check.warn_days_before == 14

    def test_runbooks(self, registry: ProjectRegistry) -> None:
        p = registry.get("test-project")
        assert p is not None
        assert len(p.runbooks) == 2
        assert p.runbooks[0].command == "make deploy"
        assert p.runbooks[1].url == "https://docs.example.com"

    def test_all_health_checks(self, registry: ProjectRegistry) -> None:
        checks = registry.all_health_checks()
        assert len(checks) == 2
        project, check = checks[0]
        assert project.id == "test-project"
        assert check.id == "api-health"

    def test_to_dict(self, registry: ProjectRegistry) -> None:
        dicts = registry.to_dict()
        assert len(dicts) == 2
        d = dicts[0]
        assert d["id"] == "test-project"
        assert d["name"] == "Test Project"
        assert len(d["envs"]) == 2
        assert len(d["health_checks"]) == 2

    def test_reload(self, registry: ProjectRegistry, sample_yaml: Path) -> None:
        # Modify file
        data = yaml.safe_load(sample_yaml.read_text())
        data["projects"].append({"id": "new-one", "name": "New"})
        sample_yaml.write_text(yaml.dump(data))

        registry.reload()
        assert registry.get("new-one") is not None

    def test_simple_project_defaults(self, registry: ProjectRegistry) -> None:
        p = registry.get("simple-project")
        assert p is not None
        assert p.envs == []
        assert p.health_checks == []
        assert p.runbooks == []
        assert p.tags == []

    def test_empty_yaml(self, tmp_path: Path) -> None:
        yml = tmp_path / "empty.yaml"
        yml.write_text("projects: []")
        reg = ProjectRegistry(path=yml)
        projects = reg.load()
        assert projects == []
