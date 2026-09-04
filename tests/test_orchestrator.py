"""Tests for the orchestrator's options contract. No API calls."""

import os
import tempfile
from pathlib import Path

from jarvis.agents import RESERVED_NAMES
from jarvis.orchestrator import (
    DEFAULT_MAX_BUDGET_USD,
    DEFAULT_MAX_TURNS,
    MAX_BUDGET_ENV,
    build_orchestrator_options,
)
from jarvis.registry import AgentRegistry
from jarvis.storage import JSONStore


def _deps():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-orch-"))
    return (
        JSONStore(tmp / "plate.json"),
        AgentRegistry(tmp / "agents.json", reserved=RESERVED_NAMES),
    )


def test_every_run_is_capped_by_default():
    store, registry = _deps()
    options = build_orchestrator_options(store, registry)
    assert options.max_turns == DEFAULT_MAX_TURNS
    assert options.max_budget_usd == DEFAULT_MAX_BUDGET_USD


def test_caps_can_come_from_the_environment():
    store, registry = _deps()
    os.environ[MAX_BUDGET_ENV] = "1.25"
    try:
        options = build_orchestrator_options(store, registry)
        assert options.max_budget_usd == 1.25
        # A garbage value falls back rather than crashing or uncapping.
        os.environ[MAX_BUDGET_ENV] = "lots"
        options = build_orchestrator_options(store, registry)
        assert options.max_budget_usd == DEFAULT_MAX_BUDGET_USD
    finally:
        del os.environ[MAX_BUDGET_ENV]


def test_resume_threads_the_session_through():
    store, registry = _deps()
    assert build_orchestrator_options(store, registry).resume is None
    options = build_orchestrator_options(store, registry, resume="sess-1")
    assert options.resume == "sess-1"


def test_a_nonpositive_explicit_cap_falls_back_to_default():
    store, registry = _deps()
    # 0 would mean "unlimited" to the SDK — exactly what the cap must prevent.
    options = build_orchestrator_options(store, registry, max_turns=0, max_budget_usd=-1)
    assert options.max_turns == DEFAULT_MAX_TURNS
    assert options.max_budget_usd == DEFAULT_MAX_BUDGET_USD


# --- the model must know what day it is --------------------------------------

def test_the_system_prompt_states_todays_date():
    """A model has no clock. Untold, it infers the date from the conversation,
    so a resumed session keeps answering as if it were the day it started."""
    from datetime import date
    from jarvis.orchestrator import build_system_prompt

    prompt = build_system_prompt({}, today=date(2026, 9, 4))
    assert "2026-09-04" in prompt
    assert "Friday" in prompt and "September 4" in prompt


def test_every_specialist_prompt_carries_the_date():
    """Built-in definitions are module-level and frozen at import, so the date
    has to be stamped when they are handed out, not when they are defined."""
    from datetime import date
    from jarvis.orchestrator import available_agents

    agents, _ = available_agents(today=date(2026, 9, 4))
    assert agents, "expected at least one agent"
    for name, definition in agents.items():
        assert "2026-09-04" in (definition.prompt or ""), f"{name} has no date"


def test_the_date_is_recomputed_per_call_not_frozen_at_import():
    from datetime import date
    from jarvis.orchestrator import available_agents

    first, _ = available_agents(today=date(2026, 9, 4))
    second, _ = available_agents(today=date(2026, 9, 5))
    name = next(iter(first))
    assert "2026-09-04" in first[name].prompt
    assert "2026-09-05" in second[name].prompt


def test_stamping_the_date_does_not_disturb_an_agents_tools():
    from jarvis.orchestrator import available_agents

    agents, _ = available_agents()
    for definition in agents.values():
        assert definition.tools, "stamping must not drop the tool allowlist"
