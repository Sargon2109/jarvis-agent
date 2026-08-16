"""Jarvis's specialist agents.

Three tiers, all of them plain :class:`AgentDefinition` data:

* **Domain specialists** — one per area the user actually works in, with
  hand-written prompts (:mod:`jarvis.agents.domains`).
* **Utility agents** — researcher and writer, for generic subtasks.
* **The generalist** — the catch-all that guarantees nothing is ever dropped.

Custom agents added later live in the persistent registry
(:mod:`jarvis.registry`) and are merged with these at startup.
"""

from claude_agent_sdk import AgentDefinition

from .domains import DOMAIN_AGENTS
from .generalist import generalist_agent
from .researcher import researcher_agent
from .writer import writer_agent

#: The catch-all agent's name — referenced by the orchestrator's routing rules.
GENERALIST = "generalist"

#: Every agent that ships in code, by name.
BUILTIN_AGENTS: dict[str, AgentDefinition] = {
    **{name: definition for name, definition in DOMAIN_AGENTS.values()},
    GENERALIST: generalist_agent,
    "researcher": researcher_agent,
    "writer": writer_agent,
}

#: Item.domain -> agent name, for the built-in domain specialists.
BUILTIN_DOMAIN_MAP: dict[str, str] = {
    domain: name for domain, (name, _) in DOMAIN_AGENTS.items()
}

#: Names a custom agent may never take.
RESERVED_NAMES: frozenset[str] = frozenset(BUILTIN_AGENTS)

__all__ = [
    "AgentDefinition",
    "BUILTIN_AGENTS",
    "BUILTIN_DOMAIN_MAP",
    "GENERALIST",
    "RESERVED_NAMES",
    "generalist_agent",
    "researcher_agent",
    "writer_agent",
]
