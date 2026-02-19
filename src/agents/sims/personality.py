"""Sims-inspired personality trait system for agents.

Each agent has a set of weighted traits that influence their behavior,
prompt style, and decision-making priorities.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum


class Trait(str, Enum):
    """Personality traits that shape agent behavior."""

    # Work style
    PERFECTIONIST = "perfectionist"      # Spends more time, higher quality
    PRAGMATIC = "pragmatic"              # Fast, good-enough solutions
    CREATIVE = "creative"                # Novel approaches, experiments
    METHODICAL = "methodical"            # Step-by-step, documented

    # Social
    COLLABORATIVE = "collaborative"      # Seeks input, shares context
    INDEPENDENT = "independent"          # Works alone, minimal chatter
    MENTOR = "mentor"                    # Explains decisions, teaches
    CHALLENGER = "challenger"            # Questions assumptions

    # Risk
    BOLD = "bold"                        # Tries new tech, big refactors
    CAUTIOUS = "cautious"               # Prefers proven patterns
    EXPERIMENTAL = "experimental"        # A/B tests, feature flags

    # Specialty affinity
    DESIGN_MINDED = "design_minded"      # Cares about UX/UI
    PERF_OBSESSED = "perf_obsessed"      # Optimizes everything
    SECURITY_FIRST = "security_first"    # Checks OWASP, sanitizes input
    TEST_DRIVEN = "test_driven"          # Writes tests before code


@dataclass
class Personality:
    """An agent's personality: a weighted set of traits."""

    traits: dict[Trait, float] = field(default_factory=dict)
    name: str = ""

    def dominant_traits(self, top_n: int = 3) -> list[Trait]:
        """Return the strongest traits."""
        sorted_traits = sorted(self.traits.items(), key=lambda x: x[1], reverse=True)
        return [t for t, _ in sorted_traits[:top_n]]

    def trait_strength(self, trait: Trait) -> float:
        return self.traits.get(trait, 0.0)

    def to_prompt_fragment(self) -> str:
        """Generate a personality description for injection into system prompts."""
        if not self.traits:
            return ""
        dominant = self.dominant_traits(3)
        descriptions = {
            Trait.PERFECTIONIST: "You are meticulous and produce high-quality, well-polished work.",
            Trait.PRAGMATIC: "You prioritize practical, working solutions over theoretical perfection.",
            Trait.CREATIVE: "You enjoy novel approaches and creative problem-solving.",
            Trait.METHODICAL: "You work step-by-step with clear documentation.",
            Trait.COLLABORATIVE: "You actively share context and seek input from others.",
            Trait.INDEPENDENT: "You work efficiently on your own with minimal overhead.",
            Trait.MENTOR: "You explain your reasoning and help others learn.",
            Trait.CHALLENGER: "You question assumptions and push for better solutions.",
            Trait.BOLD: "You're willing to try new technologies and bold refactors.",
            Trait.CAUTIOUS: "You prefer proven, battle-tested patterns.",
            Trait.EXPERIMENTAL: "You like to experiment and validate with data.",
            Trait.DESIGN_MINDED: "You care deeply about user experience and interface design.",
            Trait.PERF_OBSESSED: "You optimize for performance in everything you build.",
            Trait.SECURITY_FIRST: "You prioritize security and defensive coding.",
            Trait.TEST_DRIVEN: "You believe in test-driven development.",
        }
        lines = [descriptions.get(t, str(t)) for t in dominant]
        return "Your personality:\n" + "\n".join(f"- {l}" for l in lines)

    @classmethod
    def for_frontend(cls) -> Personality:
        return cls(
            name="Frontend Specialist",
            traits={
                Trait.DESIGN_MINDED: 0.9,
                Trait.CREATIVE: 0.8,
                Trait.PERFECTIONIST: 0.7,
                Trait.COLLABORATIVE: 0.6,
                Trait.TEST_DRIVEN: 0.4,
            },
        )

    @classmethod
    def for_backend(cls) -> Personality:
        return cls(
            name="Backend Specialist",
            traits={
                Trait.METHODICAL: 0.9,
                Trait.SECURITY_FIRST: 0.8,
                Trait.PERF_OBSESSED: 0.7,
                Trait.PRAGMATIC: 0.6,
                Trait.CAUTIOUS: 0.5,
            },
        )

    @classmethod
    def for_tester(cls) -> Personality:
        return cls(
            name="QA Specialist",
            traits={
                Trait.TEST_DRIVEN: 0.95,
                Trait.METHODICAL: 0.85,
                Trait.CHALLENGER: 0.8,
                Trait.PERFECTIONIST: 0.7,
                Trait.SECURITY_FIRST: 0.6,
            },
        )

    @classmethod
    def for_manager(cls) -> Personality:
        return cls(
            name="Project Manager",
            traits={
                Trait.COLLABORATIVE: 0.9,
                Trait.PRAGMATIC: 0.85,
                Trait.MENTOR: 0.7,
                Trait.METHODICAL: 0.65,
                Trait.BOLD: 0.4,
            },
        )

    @classmethod
    def random(cls, name: str = "Random") -> Personality:
        """Generate a random personality (useful for testing)."""
        traits = {t: round(random.uniform(0.1, 1.0), 2) for t in random.sample(list(Trait), 5)}
        return cls(name=name, traits=traits)
