"""SDK tools that let Jarvis grow its own roster of specialists.

These are granted to the **orchestrator only**, never to specialists — a
specialist doing its work should not be able to reshape the system that dispatches
it. Growing the roster is a decision, and decisions belong to the orchestrator
acting on the user's explicit say-so.

Two tools, matching the promotion design in :mod:`jarvis.promotion`:
  * ``propose_agents`` — read-only; which recurring areas have earned a specialist.
  * ``create_agent`` — writes a new specialist to the registry, after the user agrees.

``create_agent`` is deliberately not a free hand: the registry validates the name,
rejects anything shadowing a built-in, and constrains tools to the allowlist, so
the worst case is a redundant agent — undone with ``jarvis forget-agent``.
"""

from __future__ import annotations

from typing import Optional

from claude_agent_sdk import SdkMcpTool, tool

from .promotion import (
    DEFAULT_THRESHOLD,
    draft_description,
    draft_prompt,
    find_candidate,
    find_candidates,
    render_candidates,
)
from .registry import AgentRegistry, RegistryError
from .storage import Store, StoreError
from .tools import qualified

#: Tools the orchestrator alone may hold.
AGENT_TOOL_NAMES = ("propose_agents", "create_agent")


def agent_tool_names() -> list[str]:
    """Fully-qualified agent-management tool names."""
    return [qualified(name) for name in AGENT_TOOL_NAMES]


def _text(message: str, *, is_error: bool = False) -> dict:
    result: dict = {"content": [{"type": "text", "text": message}]}
    if is_error:
        result["is_error"] = True
    return result


def build_agent_tools(
    store: Store,
    registry: AgentRegistry,
    covered_domains: Optional[list[str]] = None,
) -> list[SdkMcpTool]:
    """Create the agent-management tools bound to this store and registry.

    ``covered_domains`` is the set of domains that already have an owner; it's
    passed in rather than recomputed so the tools agree exactly with the routing
    table in the orchestrator's prompt.
    """
    covered = list(covered_domains or [])

    @tool(
        "propose_agents",
        "Check which recurring areas of the user's life have no specialist yet and "
        "have come up often enough to deserve one. Use this when the user asks what "
        "agents they should add, or when you notice a repeated theme. Read-only.",
        {
            "type": "object",
            "properties": {
                "threshold": {
                    "type": "integer",
                    "description": f"How many mentions before proposing (default {DEFAULT_THRESHOLD}).",
                }
            },
            "required": [],
        },
    )
    async def propose_agents(args: dict) -> dict:
        try:
            items = store.all()
        except StoreError as exc:
            return _text(f"Could not read your plate: {exc}", is_error=True)
        threshold = args.get("threshold") or DEFAULT_THRESHOLD
        try:
            threshold = max(1, int(threshold))
        except (TypeError, ValueError):
            threshold = DEFAULT_THRESHOLD
        candidates = find_candidates(items, covered, threshold=threshold)
        if not candidates:
            return _text(
                "No area has recurred often enough to need its own specialist yet. "
                "The generalist is covering everything uncovered."
            )
        return _text(
            "These areas keep coming up and have no specialist:\n"
            + render_candidates(candidates)
            + "\n\nAsk the user whether they want a specialist for any of these "
            "before creating one."
        )

    @tool(
        "create_agent",
        "Create a new specialist agent for a recurring area. ONLY call this after "
        "the user has explicitly agreed to it in this conversation. The new agent "
        "becomes available on the next run.",
        {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "The item domain this agent will own, e.g. 'debate'.",
                },
                "name": {
                    "type": "string",
                    "description": "Agent name (letters, digits, dashes, underscores). Defaults to the domain.",
                },
                "description": {
                    "type": "string",
                    "description": "When the orchestrator should route to it. Optional — one is drafted if omitted.",
                },
                "prompt": {
                    "type": "string",
                    "description": "How the agent should behave. Optional — one is drafted from real captured items if omitted.",
                },
            },
            "required": ["domain"],
        },
    )
    async def create_agent(args: dict) -> dict:
        domain = (args.get("domain") or "").strip().lower()
        if not domain:
            return _text("A domain is required to create an agent.", is_error=True)

        try:
            items = store.all()
        except StoreError as exc:
            return _text(f"Could not read your plate: {exc}", is_error=True)

        candidate = find_candidate(items, covered, domain)
        titles = candidate.titles if candidate else []

        name = (args.get("name") or domain).strip().lower().replace(" ", "-")
        description = (args.get("description") or "").strip() or draft_description(domain)
        prompt = (args.get("prompt") or "").strip() or draft_prompt(domain, titles)
        reason = (
            f"promoted after {candidate.count} items in '{domain}'"
            if candidate
            else f"created on request for '{domain}'"
        )

        try:
            record = registry.add(
                name=name,
                description=description,
                prompt=prompt,
                domain=domain,
                reason=reason,
            )
        except RegistryError as exc:
            return _text(f"Could not create that agent: {exc}", is_error=True)

        return _text(
            f"Created the '{record.name}' specialist for the '{domain}' domain. "
            "It takes over that domain on the next run. "
            "Remove it any time with `jarvis forget-agent " + record.name + "`."
        )

    return [propose_agents, create_agent]
