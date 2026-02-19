"""Backend specialist agent.

Handles: API design, database schemas, server logic, authentication,
performance, security, infrastructure code.
"""

from __future__ import annotations

from src.agents.base import BaseAgent
from src.agents.sims.personality import Personality
from src.config import settings


class BackendAgent(BaseAgent):
    agent_type = "backend"
    default_model = settings.default_model

    def __init__(self, **kwargs):
        if "personality" not in kwargs:
            kwargs["personality"] = Personality.for_backend()
        super().__init__(**kwargs)

    def _role_description(self) -> str:
        return """You are a senior backend engineer specializing in server-side development.

Your expertise:
- Python (FastAPI, Django), Node.js (Express, tRPC)
- PostgreSQL, Redis, SQLite — schema design and query optimization
- REST API design, GraphQL, WebSockets
- Authentication/authorization (JWT, OAuth2, sessions)
- Security best practices (input validation, CORS, rate limiting, OWASP)
- Docker, CI/CD, deployment pipelines
- Background jobs, queues, caching strategies

When given a task:
1. Design the data model and API contract first
2. Implement with proper error handling and validation
3. Consider security implications at every layer
4. Write efficient queries and use appropriate indexes
5. Follow the project's existing patterns

Output format: Provide complete file contents with clear file paths."""

    def design_api(self, spec: str) -> str:
        return self.chat(
            f"Design the API for the following feature:\n\n{spec}",
            task_context=f"API design: {spec[:200]}",
        )

    def build_endpoint(self, spec: str) -> str:
        return self.chat(
            f"Implement the following backend feature:\n\n{spec}",
            task_context=f"backend implementation: {spec[:200]}",
        )

    def design_schema(self, requirements: str) -> str:
        return self.chat(
            f"Design the database schema for:\n\n{requirements}",
            task_context=f"schema design: {requirements[:200]}",
        )

    def review_security(self, code: str) -> str:
        return self.chat(
            f"Review this backend code for security vulnerabilities:\n\n```\n{code}\n```",
            task_context="security review",
        )
