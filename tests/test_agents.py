"""Tests for the built-in specialists and their wiring. No API calls."""

from jarvis.agents import (
    BUILTIN_AGENTS,
    BUILTIN_DOMAIN_MAP,
    GENERALIST,
    RESERVED_NAMES,
)
from jarvis.agents.base import ALLOWED_TOOLS, STORE_TOOLS, build_agent
from jarvis.models import SUGGESTED_DOMAINS


def test_every_user_domain_has_an_owner_or_falls_to_generalist():
    """Each suggested domain either routes to a specialist or is 'other'."""
    for domain in SUGGESTED_DOMAINS:
        if domain == "other":
            continue  # 'other' is exactly what the generalist is for
        assert domain in BUILTIN_DOMAIN_MAP, f"{domain} has no specialist"


def test_generalist_exists_as_the_safety_net():
    assert GENERALIST in BUILTIN_AGENTS
    description = BUILTIN_AGENTS[GENERALIST].description.lower()
    assert "fallback" in description or "doesn't have a dedicated" in description


def test_domain_map_points_at_real_agents():
    for domain, name in BUILTIN_DOMAIN_MAP.items():
        assert name in BUILTIN_AGENTS, f"{domain} routes to missing agent {name}"


def test_all_builtin_tools_are_within_the_allowlist():
    for name, definition in BUILTIN_AGENTS.items():
        unknown = set(definition.tools or []) - ALLOWED_TOOLS
        assert not unknown, f"{name} holds disallowed tools: {unknown}"


def test_domain_agents_can_reach_the_store():
    for domain, name in BUILTIN_DOMAIN_MAP.items():
        definition = BUILTIN_AGENTS[name]
        assert set(STORE_TOOLS) <= set(definition.tools or []), f"{name} lacks store tools"
        assert definition.mcpServers, f"{name} has no mcpServers entry"


def test_shared_conventions_applied_to_every_builtin():
    for name, definition in BUILTIN_AGENTS.items():
        if name in ("researcher", "writer"):
            continue  # utility agents predate the shared conventions
        assert "scratch" in definition.prompt, f"{name} missing sandbox convention"


def test_reserved_names_cover_all_builtins():
    assert RESERVED_NAMES == frozenset(BUILTIN_AGENTS)


def test_build_agent_rejects_tools_outside_allowlist():
    raised = False
    try:
        build_agent(description="d", prompt="p", tools=("Bash",))
    except ValueError:
        raised = True
    assert raised


def test_build_agent_without_store_has_no_mcp_servers():
    definition = build_agent(description="d", prompt="p", with_store=False)
    assert definition.mcpServers is None
    assert not (set(STORE_TOOLS) & set(definition.tools or []))
