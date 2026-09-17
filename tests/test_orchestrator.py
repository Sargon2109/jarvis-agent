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


# --- work products must survive the trip back --------------------------------

def test_specialists_are_told_the_deliverable_is_a_file():
    """A specialist that compresses a draft into its 2-4 sentence report
    produces nothing: the reply is transient and the draft never existed."""
    from jarvis.agents.base import SHARED_CONVENTIONS

    assert "write it IN FULL to a file" in SHARED_CONVENTIONS
    assert "receipt, not the work itself" in SHARED_CONVENTIONS


def test_specialists_are_told_to_carry_source_detail_into_the_file():
    from jarvis.agents.base import SHARED_CONVENTIONS

    assert "nobody after you can see it" in SHARED_CONVENTIONS


def test_the_orchestrator_is_told_not_to_split_reading_from_writing():
    """Each Task starts a blank context, so 'read the rubric' and 'write the
    paper' as separate calls leaves the writer working from a paraphrase."""
    from jarvis.orchestrator import build_system_prompt

    prompt = build_system_prompt({})
    assert "blank context" in prompt
    assert "never split" in prompt.lower()
    assert "put those paths in the instruction" in prompt


def test_every_specialist_carries_conventions_of_some_kind():
    """writer and researcher were raw AgentDefinitions that bypassed
    build_agent, so they carried no conventions at all — and writer is the
    agent most likely to be handed a draft."""
    from jarvis.orchestrator import available_agents

    agents, _ = available_agents()
    produces = "receipt, not the work itself"
    readonly = "only your reply"
    for name, definition in agents.items():
        prompt = definition.prompt or ""
        assert produces in prompt or readonly in prompt, name


def test_the_writer_carries_the_file_conventions():
    from jarvis.agents.writer import writer_agent

    assert "receipt, not the work itself" in writer_agent.prompt


def test_the_read_only_researcher_is_told_its_reply_is_all_that_survives():
    from jarvis.agents.researcher import researcher_agent

    assert "only your reply" in researcher_agent.prompt
    assert "Write" not in (researcher_agent.tools or [])


def test_the_writer_is_steered_away_from_coursework():
    """It has no Canvas tools, so a rubric-graded draft handed to it is
    written against a guess."""
    from jarvis.agents.writer import writer_agent

    assert "canvas specialist" in writer_agent.description
    assert not any("canvas" in t for t in (writer_agent.tools or []))
