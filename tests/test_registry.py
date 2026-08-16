"""Tests for the persistent agent registry. No API calls."""

import tempfile
from pathlib import Path

from jarvis.agents import BUILTIN_AGENTS, RESERVED_NAMES
from jarvis.agents.base import FILE_TOOLS, STORE_TOOLS
from jarvis.orchestrator import available_agents, build_system_prompt
from jarvis.registry import AgentRegistry, RegistryError


def _registry() -> AgentRegistry:
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-reg-"))
    return AgentRegistry(tmpdir / "agents.json", reserved=RESERVED_NAMES)


# --- creation ----------------------------------------------------------------

def test_add_persists_and_reloads_as_definition():
    reg = _registry()
    reg.add("debate", "Debate prep.", "You are the debate specialist.", domain="debate")

    reopened = AgentRegistry(reg.path, reserved=RESERVED_NAMES)
    assert reopened.get("debate") is not None
    definition = reopened.definitions()["debate"]
    assert "debate specialist" in definition.prompt
    # Shared conventions are re-applied on rebuild, not stored twice.
    assert "scratch" in definition.prompt


def test_new_agent_appears_in_available_agents_and_routing():
    reg = _registry()
    reg.add("debate", "Debate prep.", "You are the debate specialist.", domain="debate")
    agents, domain_map = available_agents(reg)
    assert "debate" in agents
    assert domain_map["debate"] == "debate"
    # ...and shows up in the orchestrator's routing table.
    assert "debate -> debate" in build_system_prompt(domain_map)


def test_custom_agent_gets_store_tools_by_default():
    reg = _registry()
    record = reg.add("debate", "Debate prep.", "You are the debate specialist.")
    assert set(STORE_TOOLS) <= set(record.tools)


# --- safety rules ------------------------------------------------------------

def test_cannot_shadow_a_builtin_name():
    reg = _registry()
    for name in list(BUILTIN_AGENTS)[:3]:
        raised = False
        try:
            reg.add(name, "d", "p")
        except RegistryError:
            raised = True
        assert raised, f"{name} should be reserved"


def test_rejects_tools_outside_allowlist():
    reg = _registry()
    raised = False
    try:
        reg.add("shell", "d", "p", tools=["Bash"])
    except RegistryError:
        raised = True
    assert raised
    assert reg.records() == []


def test_rejects_bad_names_and_empty_prompts():
    reg = _registry()
    for name, desc, prompt in [("bad name!", "d", "p"), ("ok", "", "p"), ("ok", "d", "  ")]:
        raised = False
        try:
            reg.add(name, desc, prompt)
        except RegistryError:
            raised = True
        assert raised, f"({name!r}, {desc!r}, {prompt!r}) should be rejected"


def test_rejects_duplicate_names():
    reg = _registry()
    reg.add("debate", "d", "p")
    raised = False
    try:
        reg.add("debate", "d2", "p2")
    except RegistryError:
        raised = True
    assert raised
    assert len(reg.records()) == 1


# --- resilience --------------------------------------------------------------

def test_bad_record_is_skipped_not_fatal():
    """One corrupt agent must never stop Jarvis from starting."""
    reg = _registry()
    reg.add("good", "d", "p", tools=list(FILE_TOOLS))
    # Hand-corrupt the file with an agent holding a forbidden tool.
    text = reg.path.read_text(encoding="utf-8").replace('"Read"', '"Bash"', 1)
    reg.path.write_text(text, encoding="utf-8")
    definitions = reg.definitions()  # must not raise
    assert "good" not in definitions


def test_corrupt_registry_file_raises_registry_error():
    reg = _registry()
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    reg.path.write_text("{ not json", encoding="utf-8")
    raised = False
    try:
        reg.records()
    except RegistryError:
        raised = True
    assert raised


def test_missing_registry_file_is_empty_not_an_error():
    assert _registry().records() == []


# --- removal -----------------------------------------------------------------

def test_remove_deletes_custom_agent():
    reg = _registry()
    reg.add("debate", "d", "p")
    assert reg.remove("debate") is True
    assert reg.records() == []
    assert reg.remove("debate") is False
