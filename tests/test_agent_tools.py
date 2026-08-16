"""Tests for the orchestrator-only agent-management tools. No API calls."""

import asyncio
import tempfile
from pathlib import Path

from jarvis.agent_tools import agent_tool_names, build_agent_tools
from jarvis.agents import RESERVED_NAMES
from jarvis.agents.base import ALLOWED_TOOLS
from jarvis.registry import AgentRegistry
from jarvis.storage import JSONStore


def _env():
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-at-"))
    store = JSONStore(tmpdir / "plate.json")
    registry = AgentRegistry(tmpdir / "agents.json", reserved=RESERVED_NAMES)
    return store, registry


def _handlers(store, registry, covered=()):
    return {t.name: t.handler for t in build_agent_tools(store, registry, list(covered))}


def _run(coro):
    return asyncio.run(coro)


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _seed(store, domain, n):
    for i in range(n):
        store.add(f"{domain} item {i}", domain=domain)


# --- propose -----------------------------------------------------------------

def test_propose_is_quiet_when_nothing_recurs():
    store, registry = _env()
    _seed(store, "debate", 1)
    result = _run(_handlers(store, registry)["propose_agents"]({}))
    assert "No area has recurred" in _text(result)


def test_propose_surfaces_a_recurring_domain():
    store, registry = _env()
    _seed(store, "debate", 4)
    text = _text(_run(_handlers(store, registry)["propose_agents"]({})))
    assert "debate" in text
    assert "Ask the user" in text  # must not create on its own initiative


def test_propose_skips_domains_that_already_have_an_owner():
    store, registry = _env()
    _seed(store, "club", 5)
    result = _run(_handlers(store, registry, covered=["club"])["propose_agents"]({}))
    assert "No area has recurred" in _text(result)


def test_propose_honours_a_custom_threshold():
    store, registry = _env()
    _seed(store, "debate", 2)
    handlers = _handlers(store, registry)
    assert "No area has recurred" in _text(_run(handlers["propose_agents"]({})))
    assert "debate" in _text(_run(handlers["propose_agents"]({"threshold": 2})))


def test_propose_survives_a_nonsense_threshold():
    store, registry = _env()
    _seed(store, "debate", 4)
    result = _run(_handlers(store, registry)["propose_agents"]({"threshold": "lots"}))
    assert not result.get("is_error")


# --- create ------------------------------------------------------------------

def test_create_agent_persists_and_grounds_its_prompt():
    store, registry = _env()
    _seed(store, "debate", 3)
    result = _run(_handlers(store, registry)["create_agent"]({"domain": "debate"}))
    assert not result.get("is_error")

    record = registry.get("debate")
    assert record is not None and record.domain == "debate"
    assert "debate item 2" in record.prompt      # grounded in real captured work
    assert "promoted after 3 items" in record.reason


def test_create_agent_accepts_an_explicit_prompt_and_name():
    store, registry = _env()
    _run(_handlers(store, registry)["create_agent"]({
        "domain": "debate", "name": "debate-coach",
        "description": "Debate work.", "prompt": "You are a debate coach.",
    }))
    record = registry.get("debate-coach")
    assert record is not None and record.prompt.startswith("You are a debate coach.")


def test_create_agent_refuses_to_shadow_a_builtin():
    store, registry = _env()
    result = _run(_handlers(store, registry)["create_agent"]({"domain": "x", "name": "club"}))
    assert result.get("is_error") is True
    assert registry.records() == []


def test_create_agent_requires_a_domain():
    store, registry = _env()
    result = _run(_handlers(store, registry)["create_agent"]({}))
    assert result.get("is_error") is True


def test_create_agent_rejects_a_duplicate():
    store, registry = _env()
    handlers = _handlers(store, registry)
    _run(handlers["create_agent"]({"domain": "debate"}))
    result = _run(handlers["create_agent"]({"domain": "debate"}))
    assert result.get("is_error") is True
    assert len(registry.records()) == 1


def test_created_agent_holds_only_allowlisted_tools():
    """A promoted agent can never end up with powers a built-in wouldn't have."""
    store, registry = _env()
    _run(_handlers(store, registry)["create_agent"]({"domain": "debate"}))
    definition = registry.definitions()["debate"]
    assert set(definition.tools or []) <= ALLOWED_TOOLS


def test_created_agent_cannot_create_further_agents():
    """Only the orchestrator grows the roster — specialists must not."""
    store, registry = _env()
    _run(_handlers(store, registry)["create_agent"]({"domain": "debate"}))
    definition = registry.definitions()["debate"]
    assert not (set(definition.tools or []) & set(agent_tool_names()))


# --- naming ------------------------------------------------------------------

def test_agent_tool_names_are_namespaced():
    assert agent_tool_names() == [
        "mcp__jarvis_store__propose_agents",
        "mcp__jarvis_store__create_agent",
    ]
