"""The Jarvis orchestrator: turns a thought-dump into remembered, delegated work.

:func:`build_orchestrator_options` assembles the :class:`ClaudeAgentOptions` that
wire together the store tools, every available specialist (built-in plus anything
in the persistent registry), and the system prompt that tells Jarvis to *capture
first, then delegate*.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import date
from typing import Optional

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

from .agents import (
    BUILTIN_AGENTS,
    BUILTIN_DOMAIN_MAP,
    GENERALIST,
    RESERVED_NAMES,
)
from .agent_tools import agent_tool_names, build_agent_tools
from .agents.base import NET_TOOLS
from .registry import AgentRegistry
from .storage import Store, create_store
from .canvas_tools import (
    SERVER_NAME as CANVAS_SERVER_NAME,
    build_canvas_server,
    canvas_tool_names,
)
from .tools import SERVER_NAME, build_store_server, store_tool_names

#: The model driving the orchestrator itself.
ORCHESTRATOR_MODEL = "claude-sonnet-5"

#: Caps on a single run. A hung loop or a runaway delegation chain should cost
#: a bounded amount of money and time, never "whatever it takes". Generous —
#: the six-specialist README example fits comfortably — but finite.
DEFAULT_MAX_TURNS = 100
DEFAULT_MAX_BUDGET_USD = 3.00

#: Env overrides for the caps, so the budget is a setting, not a code edit.
MAX_TURNS_ENV = "JARVIS_MAX_TURNS"
MAX_BUDGET_ENV = "JARVIS_MAX_BUDGET_USD"

#: Ceiling on one message between the SDK and its CLI subprocess. The SDK
#: defaults to 1MB, which a single Read of a large course PDF blows straight
#: through — and the failure kills the whole run mid-way, losing the work done
#: so far. Course material is routinely multi-megabyte (a 2.7MB slide deck is
#: unremarkable), so the default is far too tight for this workload.
#:
#: This is a safety net, not the real defense: the file tools cap what they
#: hand back so a result never gets near this in the first place.
DEFAULT_MAX_BUFFER_BYTES = 24 * 1024 * 1024
MAX_BUFFER_ENV = "JARVIS_MAX_BUFFER_BYTES"


def _env_cap(env: str, default: float, cast) -> float:
    """Read a numeric cap from the environment, falling back on bad values."""
    raw = os.environ.get(env)
    if not raw:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _positive(explicit, env: str, default: float, cast):
    """Resolve a run cap: explicit arg, then env, then default — but a
    non-positive value from ANY source means "no cap", which is exactly what
    these caps exist to prevent, so it falls back to the default instead."""
    if explicit is not None:
        try:
            value = cast(explicit)
        except (TypeError, ValueError):
            return cast(default)
        return cast(value) if value > 0 else cast(default)
    return cast(_env_cap(env, default, cast))


def today_line(today: Optional[date] = None) -> str:
    """The one sentence that tells a model what day it is.

    A model has no clock. Left untold, it infers the date from whatever the
    conversation implies — so a resumed session still believes it is the day
    that session started, and "due tomorrow" quietly means the wrong tomorrow.
    Every prompt is stamped with this, rebuilt per run so a desk left open for
    days stays correct.
    """
    today = today or date.today()
    return (
        f"Today's date is {today.strftime('%A, %B %-d, %Y')} "
        f"({today.isoformat()}).\n"
        "This conversation may span several days. This date is authoritative: "
        "ignore any earlier statement in the history about what today is, and "
        "recompute anything relative — 'tomorrow', 'this week', how overdue "
        "something is — from the date above.\n\n"
    )


def available_agents(
    registry: Optional[AgentRegistry] = None,
    *,
    today: Optional[date] = None,
) -> tuple[dict[str, AgentDefinition], dict[str, str]]:
    """All agents (built-in + registry) and the combined domain -> agent map.

    Definitions are stamped with the current date as they are handed out. The
    built-in definitions are module-level and therefore frozen at import, so
    stamping them here — not at definition time — is what keeps a long-running
    server from serving yesterday's date forever.
    """
    registry = registry or AgentRegistry(reserved=RESERVED_NAMES)
    agents = {**BUILTIN_AGENTS, **registry.definitions()}
    domain_map = {**BUILTIN_DOMAIN_MAP, **registry.domain_map()}
    # A domain may only route to an agent that actually exists.
    domain_map = {d: n for d, n in domain_map.items() if n in agents}
    stamp = today_line(today)
    agents = {
        name: replace(definition, prompt=stamp + (definition.prompt or ""))
        for name, definition in agents.items()
    }
    return agents, domain_map


def _routing_table(domain_map: dict[str, str]) -> str:
    lines = [f"  {domain} -> {name}" for domain, name in sorted(domain_map.items())]
    return "\n".join(lines)


def build_system_prompt(
    domain_map: dict[str, str], *, today: Optional[date] = None
) -> str:
    """The orchestrator's instructions, including the live routing table."""
    return (
        today_line(today)
        + "You are Jarvis, a personal chief of staff. You orchestrate the user's "
        "work; you do not do it yourself.\n\n"
        "When the user dumps their thoughts:\n\n"
        "1. CAPTURE FIRST. Break the dump into individual items and save each one "
        "with capture_thought before anything else. Choose a kind (project = "
        "ongoing effort, task = a to-do, reminder = time-based nudge) and a short "
        "domain. Use add_reminder for anything with a date. Nothing the user says "
        "may go unrecorded.\n\n"
        "2. REPORT. Use list_plate (or agenda, for what's due) to reflect back "
        "what's now on the user's plate.\n\n"
        "3. DELEGATE. For any item needing real work, hand it to a specialist with "
        "the Task tool. Route by the item's domain:\n"
        f"{_routing_table(domain_map)}\n\n"
        f"   Anything not in that table goes to '{GENERALIST}'. Never skip an item "
        f"because no specialist fits — that is exactly what '{GENERALIST}' is for. "
        "Never do the work yourself.\n\n"
        "4. GROW, BUT ONLY WHEN ASKED. If an area keeps recurring with no specialist, "
        "propose_agents will tell you. Mention it to the user and ask whether they "
        "want a specialist for it. Only call create_agent once they have clearly said "
        "yes in this conversation — never create one on your own initiative, and "
        "never create one the first time a new topic appears.\n\n"
        "COURSEWORK AND CANVAS. Jarvis has live, read-only access to the user's "
        "Canvas — courses, syllabi, assignments, files, announcements, modules. "
        "Anything about a class, a reading, a deadline, or an assignment goes to "
        "the 'canvas' specialist, which holds those tools and keeps a durable "
        "profile of each course. Never tell the user Jarvis cannot see Canvas, "
        "and never ask them to paste or export something the specialist can "
        "fetch. You hold the Canvas tools only so the specialist can use them — "
        "route the work, don't do it yourself.\n\n"
        "If the user is only asking what's on their plate, just report — don't "
        "re-capture or delegate. Be concise: your value is that nothing is "
        "forgotten and the right specialist picks up each piece."
    )


def build_orchestrator_options(
    store: Optional[Store] = None,
    registry: Optional[AgentRegistry] = None,
    dump_id: Optional[str] = None,
    *,
    resume: Optional[str] = None,
    max_turns: Optional[int] = None,
    max_budget_usd: Optional[float] = None,
) -> ClaudeAgentOptions:
    """Build the orchestrator's options.

    ``store`` and ``registry`` can be injected (tests do this); otherwise the
    defaults at the standard locations are used. ``dump_id`` links every item
    captured during this run back to the brain-dump it came from.

    ``resume`` continues an existing SDK session — this is what gives the
    command desk an actual conversation instead of per-message amnesia, and
    what makes "the user said yes earlier in this conversation" a state that
    can exist at all. ``max_turns``/``max_budget_usd`` default to the
    ``JARVIS_MAX_TURNS``/``JARVIS_MAX_BUDGET_USD`` env vars, then the module
    defaults; a run that hits a cap stops cleanly instead of running a tab.
    """
    store = store or create_store()
    registry = registry or AgentRegistry(reserved=RESERVED_NAMES)
    agents, domain_map = available_agents(registry)
    # The orchestrator alone gets the agent-management tools; specialists hold
    # only the store tools their own definitions grant them.
    extra = build_agent_tools(store, registry, list(domain_map))
    return ClaudeAgentOptions(
        system_prompt=build_system_prompt(domain_map),
        model=ORCHESTRATOR_MODEL,
        allowed_tools=[
            "Task", *NET_TOOLS, *store_tool_names(),
            *agent_tool_names(), *canvas_tool_names(),
        ],
        permission_mode="acceptEdits",
        # The Canvas server is registered unconditionally. Its tools resolve the
        # token lazily, so an unconfigured machine gets a clear "set
        # JARVIS_CANVAS_TOKEN" error from the tool itself — far better than the
        # tool silently not existing and the agent concluding it has no access,
        # which is exactly the failure this whole change is fixing.
        mcp_servers={
            SERVER_NAME: build_store_server(store, dump_id, extra),
            CANVAS_SERVER_NAME: build_canvas_server(),
        },
        agents=agents,
        resume=resume,
        max_turns=_positive(max_turns, MAX_TURNS_ENV, DEFAULT_MAX_TURNS, int),
        max_budget_usd=_positive(max_budget_usd, MAX_BUDGET_ENV, DEFAULT_MAX_BUDGET_USD, float),
        max_buffer_size=int(
            _positive(None, MAX_BUFFER_ENV, DEFAULT_MAX_BUFFER_BYTES, int)
        ),
    )
