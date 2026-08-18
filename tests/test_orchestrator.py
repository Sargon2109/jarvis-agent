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
