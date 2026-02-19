"""Testing specialist agent.

Handles: test strategy, unit tests, integration tests, E2E tests,
test fixtures, mocking, coverage analysis.
"""

from __future__ import annotations

from src.agents.base import BaseAgent
from src.agents.sims.personality import Personality
from src.config import settings


class TesterAgent(BaseAgent):
    agent_type = "tester"
    default_model = settings.default_model

    def __init__(self, **kwargs):
        if "personality" not in kwargs:
            kwargs["personality"] = Personality.for_tester()
        super().__init__(**kwargs)

    def _role_description(self) -> str:
        return """You are a senior QA/test engineer specializing in comprehensive testing strategies.

Your expertise:
- Python: pytest, pytest-asyncio, unittest.mock, factory_boy, hypothesis
- JavaScript/TypeScript: Vitest, Jest, Playwright, Testing Library
- Test architecture: unit, integration, E2E, contract, snapshot
- Mocking and fixtures: dependency injection, test doubles, fakes
- Coverage analysis and gap identification
- CI test pipeline optimization

When given code to test:
1. Identify the critical paths and edge cases
2. Write tests that verify behavior, not implementation
3. Use descriptive test names (test_<scenario>_<expected_outcome>)
4. Create minimal, focused fixtures
5. Mock external dependencies, not internal logic

Output format: Provide complete test file contents with clear file paths."""

    def write_tests(self, code: str, context: str = "") -> str:
        prompt = f"Write comprehensive tests for the following code:\n\n```\n{code}\n```"
        if context:
            prompt += f"\n\nAdditional context:\n{context}"
        return self.chat(prompt, task_context=f"testing: {context[:200] if context else 'code'}")

    def write_integration_tests(self, api_spec: str) -> str:
        return self.chat(
            f"Write integration tests for this API specification:\n\n{api_spec}",
            task_context="integration testing",
        )

    def analyze_coverage(self, coverage_report: str) -> str:
        return self.chat(
            f"Analyze this coverage report and suggest what additional tests are needed:\n\n{coverage_report}",
            task_context="coverage analysis",
        )

    def review_tests(self, test_code: str) -> str:
        return self.chat(
            f"Review these tests for quality, coverage gaps, and best practices:\n\n```\n{test_code}\n```",
            task_context="test review",
        )
