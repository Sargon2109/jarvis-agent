"""In-process SDK tools that expose the store to Jarvis's agents.

These wrap a :class:`~jarvis.storage.Store` in the Claude Agent SDK's ``@tool``
interface and bundle them into an in-process MCP server. Running in-process
means the tools touch the store directly — no subprocess, no IPC.

The tools are built by a **factory** bound to a specific store instance
(:func:`build_store_tools`), rather than reaching for a module-level global.
That keeps them dependency-injected and trivial to test against a temp store.
"""

from __future__ import annotations

from typing import Optional

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

from .agenda import build_agenda, render_agenda
from .models import format_item
from .storage import JSONStore, Store, StoreError

#: Name of the in-process MCP server; tools are namespaced ``mcp__<server>__<tool>``.
SERVER_NAME = "jarvis_store"

#: The tools every specialist may hold — read and update the user's plate.
STORE_TOOL_NAMES = (
    "capture_thought",
    "list_plate",
    "agenda",
    "add_reminder",
    "complete_item",
)


def qualified(name: str) -> str:
    """The MCP-namespaced form of a tool name."""
    return f"mcp__{SERVER_NAME}__{name}"


def store_tool_names() -> list[str]:
    """Fully-qualified store tools. These are what specialists are granted."""
    return [qualified(name) for name in STORE_TOOL_NAMES]


# --- result helpers ----------------------------------------------------------

def _text(message: str, *, is_error: bool = False) -> dict:
    """Build the ``{'content': [...]}`` result shape the SDK expects."""
    result: dict = {"content": [{"type": "text", "text": message}]}
    if is_error:
        result["is_error"] = True
    return result


# --- tool factory ------------------------------------------------------------

def build_store_tools(store: Store, dump_id: Optional[str] = None) -> list[SdkMcpTool]:
    """Create the store tools bound to ``store``.

    ``dump_id`` (when the run came from a recorded brain-dump) is stamped onto
    everything captured, so each item can be traced back to what you actually
    said. Returns the SdkMcpTool list.
    """

    @tool(
        "capture_thought",
        "Save one thing the user dumped so Jarvis remembers it. Call once per "
        "distinct thought. Choose kind (project = ongoing effort, task = a to-do, "
        "reminder = time-based nudge) and a short domain such as club, rebrand, "
        "startup, homework, leetcode, internships, or other.",
        {
            "type": "object",
            "properties": {
                "raw": {"type": "string", "description": "The user's own words."},
                "title": {"type": "string", "description": "Short cleaned one-line summary."},
                "kind": {
                    "type": "string",
                    "enum": ["project", "task", "reminder"],
                },
                "domain": {"type": "string", "description": "Short area label."},
                "due": {"type": "string", "description": "Optional due date, YYYY-MM-DD."},
                "agent": {"type": "string", "description": "Optional specialist agent to own it."},
            },
            "required": ["raw", "kind"],
        },
    )
    async def capture_thought(args: dict) -> dict:
        try:
            item = store.add(
                raw=args["raw"],
                title=(args.get("title") or None),
                kind=args.get("kind", "task"),
                domain=(args.get("domain") or "other"),
                due=(args.get("due") or None),
                agent=(args.get("agent") or None),
                dump_id=dump_id,
            )
        except (ValueError, KeyError, StoreError) as exc:
            return _text(f"Could not capture that: {exc}", is_error=True)
        return _text(f"Captured {format_item(item)}")

    @tool(
        "list_plate",
        "List what's on the user's plate. By default hides finished items; pass "
        "status to see a specific state. Optionally filter by kind or domain.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["project", "task", "reminder"]},
                "status": {"type": "string", "enum": ["inbox", "active", "done"]},
                "domain": {"type": "string"},
            },
            "required": [],
        },
    )
    async def list_plate(args: dict) -> dict:
        status = args.get("status") or None
        try:
            items = store.list(
                kind=(args.get("kind") or None),
                status=status,
                domain=(args.get("domain") or None),
            )
        except StoreError as exc:
            return _text(f"Could not read your plate: {exc}", is_error=True)
        if status is None:
            items = [item for item in items if item.status != "done"]
        if not items:
            return _text("Nothing on your plate that matches.")
        return _text("\n".join(format_item(item) for item in items))

    @tool(
        "agenda",
        "Show what's due — the user's dated, unfinished items grouped into "
        "overdue, today, and upcoming. Use this to answer 'what's on my plate "
        "right now?' or to surface reminders.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def agenda(args: dict) -> dict:
        try:
            items = store.all()
        except StoreError as exc:
            return _text(f"Could not read your agenda: {exc}", is_error=True)
        return _text(render_agenda(build_agenda(items)))

    @tool(
        "add_reminder",
        "Add a time-based reminder for the user. Always include a due date.",
        {
            "type": "object",
            "properties": {
                "raw": {"type": "string", "description": "What to be reminded of."},
                "due": {"type": "string", "description": "When, as YYYY-MM-DD."},
                "title": {"type": "string", "description": "Optional short summary."},
                "domain": {"type": "string", "description": "Optional area label."},
            },
            "required": ["raw", "due"],
        },
    )
    async def add_reminder(args: dict) -> dict:
        try:
            item = store.add(
                raw=args["raw"],
                title=(args.get("title") or None),
                kind="reminder",
                domain=(args.get("domain") or "other"),
                due=args["due"],
                dump_id=dump_id,
            )
        except (ValueError, KeyError, StoreError) as exc:
            return _text(f"Could not add reminder: {exc}", is_error=True)
        return _text(f"Reminder set {format_item(item)}")

    @tool(
        "complete_item",
        "Mark an item done by its id (the short code shown in square brackets).",
        {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "The item's id."}},
            "required": ["id"],
        },
    )
    async def complete_item(args: dict) -> dict:
        item_id = args.get("id", "")
        try:
            item = store.complete(item_id)
        except StoreError as exc:
            return _text(f"Could not update your plate: {exc}", is_error=True)
        if item is None:
            return _text(f"No item found with id {item_id!r}.", is_error=True)
        return _text(f"Done: {format_item(item)}")

    return [capture_thought, list_plate, agenda, add_reminder, complete_item]


def build_store_server(
    store: Optional[Store] = None,
    dump_id: Optional[str] = None,
    extra_tools: Optional[list[SdkMcpTool]] = None,
):
    """Build the in-process MCP server exposing the store tools.

    Pass a ``store`` to inject one (tests do this); defaults to a ``JSONStore``
    at the standard location. ``dump_id`` links captured items to their dump.
    ``extra_tools`` adds orchestrator-only tools (see :mod:`jarvis.agent_tools`)
    to the same server — specialists never receive them, because what an agent
    may use is decided by its own tool allowlist, not by what the server offers.
    """
    store = store or JSONStore()
    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="0.4.0",
        tools=build_store_tools(store, dump_id) + list(extra_tools or []),
    )
