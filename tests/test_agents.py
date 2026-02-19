"""Tests for the agent modules."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.agents.base import BaseAgent
from src.agents.frontend import FrontendAgent
from src.agents.backend import BackendAgent
from src.agents.tester import TesterAgent
from src.agents.manager import ManagerAgent
from src.agents.sims.personality import Personality, Trait
from src.agents.sims.drives import DriveSystem, DriveState, DriveType
from src.token_tracker.tracker import TokenTracker


class TestPersonality:
    def test_for_frontend(self):
        p = Personality.for_frontend()
        assert Trait.DESIGN_MINDED in p.dominant_traits(3)

    def test_for_backend(self):
        p = Personality.for_backend()
        assert Trait.METHODICAL in p.dominant_traits(3)

    def test_for_tester(self):
        p = Personality.for_tester()
        assert Trait.TEST_DRIVEN in p.dominant_traits(1)

    def test_for_manager(self):
        p = Personality.for_manager()
        assert Trait.COLLABORATIVE in p.dominant_traits(3)

    def test_to_prompt_fragment(self):
        p = Personality.for_frontend()
        fragment = p.to_prompt_fragment()
        assert "Your personality:" in fragment
        assert len(fragment) > 50

    def test_random_personality(self):
        p = Personality.random("Test")
        assert len(p.traits) == 5
        assert p.name == "Test"

    def test_trait_strength(self):
        p = Personality(traits={Trait.BOLD: 0.9})
        assert p.trait_strength(Trait.BOLD) == 0.9
        assert p.trait_strength(Trait.CAUTIOUS) == 0.0


class TestDriveSystem:
    def test_initial_state(self):
        ds = DriveSystem()
        assert ds.state.get(DriveType.ENERGY) == 100.0
        assert ds.state.overall_effectiveness() > 0.8

    def test_tick_decays_drives(self):
        ds = DriveSystem()
        initial_energy = ds.state.get(DriveType.ENERGY)
        ds.tick(minutes_worked=10.0)
        assert ds.state.get(DriveType.ENERGY) < initial_energy

    def test_rest_recovers_energy(self):
        ds = DriveSystem()
        ds.tick(minutes_worked=50.0)
        low_energy = ds.state.get(DriveType.ENERGY)
        ds.rest()
        assert ds.state.get(DriveType.ENERGY) > low_energy

    def test_success_boosts_morale(self):
        ds = DriveSystem()
        ds.state.levels[DriveType.MORALE] = 50.0
        ds.record_success()
        assert ds.state.get(DriveType.MORALE) == 60.0

    def test_failure_drains_morale(self):
        ds = DriveSystem()
        ds.state.levels[DriveType.MORALE] = 50.0
        ds.record_failure()
        assert ds.state.get(DriveType.MORALE) == 35.0

    def test_context_switch_drains_focus(self):
        ds = DriveSystem()
        initial_focus = ds.state.get(DriveType.FOCUS)
        ds.context_switch()
        assert ds.state.get(DriveType.FOCUS) < initial_focus

    def test_needs_rest(self):
        ds = DriveSystem()
        ds.state.levels[DriveType.ENERGY] = 15.0
        assert ds.state.needs_rest()

    def test_status_labels(self):
        ds = DriveSystem()
        assert ds.state.status_label() == "in the zone"

        ds.state.levels[DriveType.ENERGY] = 10.0
        assert ds.state.status_label() == "exhausted"

    def test_to_prompt_fragment(self):
        ds = DriveSystem()
        fragment = ds.to_prompt_fragment()
        assert "Energy:" in fragment
        assert "Focus:" in fragment

    def test_reset(self):
        ds = DriveSystem()
        ds.tick(minutes_worked=100.0)
        ds.reset()
        assert ds.state.get(DriveType.ENERGY) == 100.0

    def test_to_dict(self):
        ds = DriveSystem()
        d = ds.state.to_dict()
        assert "energy" in d
        assert "focus" in d
        assert "effectiveness" in d
        assert "status" in d

    def test_levels_clamp_to_100(self):
        ds = DriveSystem()
        ds.state.levels[DriveType.ENERGY] = 95.0
        ds.rest()  # +30 energy
        assert ds.state.get(DriveType.ENERGY) == 100.0  # clamped


class TestBaseAgent:
    def test_chat(self, tracker):
        agent = BaseAgent(agent_id="test", tracker=tracker)
        response = agent.chat("hello")
        assert response == "Hello from Claude"
        assert len(agent._conversation) == 2

    def test_system_prompt_includes_role(self, tracker):
        agent = BaseAgent(agent_id="test", tracker=tracker)
        prompt = agent.system_prompt()
        assert "base agent" in prompt

    def test_status(self, tracker):
        agent = BaseAgent(agent_id="test", tracker=tracker)
        agent.chat("hi")
        status = agent.status()
        assert status["agent_id"] == "test"
        assert status["conversation_length"] == 2
        assert "drives" in status

    def test_reset_conversation(self, tracker):
        agent = BaseAgent(agent_id="test", tracker=tracker)
        agent.chat("hi")
        agent.reset_conversation()
        assert len(agent._conversation) == 0


class TestSpecialistAgents:
    def test_frontend_agent_personality(self, tracker):
        agent = FrontendAgent(agent_id="fe", tracker=tracker)
        assert agent.personality.name == "Frontend Specialist"
        assert agent.agent_type == "frontend"

    def test_backend_agent_personality(self, tracker):
        agent = BackendAgent(agent_id="be", tracker=tracker)
        assert agent.personality.name == "Backend Specialist"
        assert agent.agent_type == "backend"

    def test_tester_agent_personality(self, tracker):
        agent = TesterAgent(agent_id="qa", tracker=tracker)
        assert agent.personality.name == "QA Specialist"
        assert agent.agent_type == "tester"

    def test_manager_agent_personality(self, tracker):
        agent = ManagerAgent(agent_id="mgr", tracker=tracker)
        assert agent.personality.name == "Project Manager"
        assert agent.agent_type == "manager"

    def test_frontend_build_component(self, tracker):
        agent = FrontendAgent(agent_id="fe", tracker=tracker)
        result = agent.build_component("A navbar with logo and links")
        assert isinstance(result, str)

    def test_backend_design_api(self, tracker):
        agent = BackendAgent(agent_id="be", tracker=tracker)
        result = agent.design_api("User CRUD endpoints")
        assert isinstance(result, str)

    def test_tester_write_tests(self, tracker):
        agent = TesterAgent(agent_id="qa", tracker=tracker)
        result = agent.write_tests("def add(a, b): return a + b")
        assert isinstance(result, str)
