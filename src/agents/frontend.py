"""Frontend specialist agent.

Handles: React/Next.js components, CSS/Tailwind, accessibility,
responsive design, client-side state, UI/UX decisions.
"""

from __future__ import annotations

from src.agents.base import BaseAgent
from src.agents.sims.personality import Personality
from src.config import settings


class FrontendAgent(BaseAgent):
    agent_type = "frontend"
    default_model = settings.default_model

    def __init__(self, **kwargs):
        if "personality" not in kwargs:
            kwargs["personality"] = Personality.for_frontend()
        super().__init__(**kwargs)

    def _role_description(self) -> str:
        return """You are a senior frontend engineer specializing in modern web development.

Your expertise:
- React, Next.js (App Router), TypeScript
- Tailwind CSS, CSS Modules, responsive design
- Accessibility (WCAG 2.1 AA), semantic HTML
- Client-side state management (React hooks, Zustand, Jotai)
- Performance optimization (code splitting, lazy loading, Core Web Vitals)
- Component architecture and design systems

When given a task:
1. Consider the component hierarchy and data flow
2. Write clean, typed TypeScript with proper interfaces
3. Ensure responsive design and accessibility
4. Include relevant ARIA attributes
5. Follow the project's existing patterns and conventions

Output format: Provide complete file contents with clear file paths."""

    def build_component(self, spec: str) -> str:
        return self.chat(
            f"Build the following frontend component:\n\n{spec}",
            task_context=f"frontend component: {spec[:200]}",
        )

    def review_ui(self, code: str) -> str:
        return self.chat(
            f"Review this frontend code for UX, accessibility, and best practices:\n\n```\n{code}\n```",
            task_context="frontend code review",
        )

    def style_component(self, component_code: str, design_spec: str) -> str:
        return self.chat(
            f"Style this component according to the design spec.\n\nComponent:\n```\n{component_code}\n```\n\nDesign spec:\n{design_spec}",
            task_context="component styling",
        )
