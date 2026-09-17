"""Shared building blocks for Jarvis's specialist agents.

Every specialist is an :class:`AgentDefinition` — plain data: a description
telling the orchestrator *when* to use it, a prompt telling it *how* to behave,
and a tool allowlist. :func:`build_agent` applies the conventions all Jarvis
agents share so individual definitions stay focused on their actual expertise.
"""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from ..canvas_tools import canvas_tool_names
from ..tools import SERVER_NAME, store_tool_names

#: File tools a specialist may use. Write/Edit are sandboxed to scratch/ by prompt.
FILE_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep", "Write", "Edit")

#: Read-only subset, for agents that should investigate but never produce files.
READONLY_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")

#: Web access. Granted narrowly — a fetched page is untrusted text that can try
#: to steer an agent, so only outward-facing specialists that genuinely need to
#: look things up should hold these. WebFetch reads a URL; WebSearch finds them.
NET_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")

#: The store tools, so a specialist can record what it discovers and close out work.
#: Deliberately the *store* tools only — a specialist can never create other
#: agents. Growing the roster is the orchestrator's job, with the user's consent.
STORE_TOOLS: tuple[str, ...] = tuple(store_tool_names())

#: Live Canvas access, read-only. Granted narrowly to coursework specialists:
#: course material is the user's own, but it is still text written by other
#: people, so only agents that genuinely need it should be able to pull it in.
CANVAS_TOOLS: tuple[str, ...] = tuple(canvas_tool_names())

#: The full set an agent is ever allowed to hold. Anything created at runtime
#: (a promoted agent) must draw its tools from here — never arbitrary power.
ALLOWED_TOOLS: frozenset[str] = (
    frozenset(FILE_TOOLS)
    | frozenset(STORE_TOOLS)
    | frozenset(NET_TOOLS)
    | frozenset(CANVAS_TOOLS)
)

#: Appended to every specialist prompt so behavior is consistent across agents.
SHARED_CONVENTIONS = (
    "\n\nHow you work:\n"
    "- You own one item at a time. Do the actual work; don't just plan it.\n"
    "- Only create or edit files inside the 'scratch' folder.\n"
    "- If you discover a genuinely new follow-up task, record it with "
    "capture_thought so it isn't lost. Don't capture trivia or restate the work "
    "you were already given.\n"
    "- When the work you were handed is finished, mark it done with complete_item.\n"
    # The summary below is a receipt, not the deliverable. Before this was
    # spelled out, an agent asked to draft something would compress the draft
    # itself into those few sentences, and the actual work never existed
    # anywhere. Anything the user is meant to read, keep, or hand in is a file.
    "- If the work produces something the user will read or submit — a draft, "
    "an essay, a plan, an outline — write it IN FULL to a file in 'scratch' "
    "first. Never let the finished thing exist only inside your reply.\n"
    "- Whatever you looked at to do the work — a rubric, a sample, a reading — "
    "keep the details that matter in that file too. You are the only one who "
    "read the source; nobody after you can see it.\n"
    "- Finish by reporting in 2-4 sentences what you did and naming the file "
    "you wrote. That report is a receipt, not the work itself."
)

#: For agents that investigate but never produce files. Same reasoning about
#: what survives the trip back — their findings ARE the deliverable, so the
#: detail has to be in the report rather than in a file.
READONLY_CONVENTIONS = (
    "\n\nHow you work:\n"
    "- Investigate what you were asked about. Do the actual reading; don't "
    "guess or summarize from the request alone.\n"
    "- You never create or edit files.\n"
    "- Whoever asked you cannot see what you read — only your reply. So put "
    "the specifics in it: the numbers, the exact wording, the citations. A "
    "vague summary of a precise source is worse than useless downstream.\n"
    "- Name your sources so the finding can be checked."
)


def build_agent(
    *,
    description: str,
    prompt: str,
    tools: tuple[str, ...] = FILE_TOOLS,
    with_store: bool = True,
    model: str = "sonnet",
    produces_files: bool = True,
) -> AgentDefinition:
    """Assemble an AgentDefinition with Jarvis's shared conventions applied.

    ``produces_files`` picks which set of conventions applies: an agent that
    produces work writes it to a file, while a read-only agent must instead
    carry the detail back in its reply, since nothing else it touched survives.
    """
    granted = tuple(tools) + (STORE_TOOLS if with_store else ())
    unknown = set(granted) - ALLOWED_TOOLS
    if unknown:
        raise ValueError(f"agent requests tools outside the allowlist: {sorted(unknown)}")
    return AgentDefinition(
        description=description,
        prompt=prompt + (SHARED_CONVENTIONS if produces_files else READONLY_CONVENTIONS),
        tools=list(granted),
        model=model,
        mcpServers=[SERVER_NAME] if with_store else None,
    )
