"""Jarvis orchestrator entry point (the LLM 'dump' flow).

Give it a brain-dump and the orchestrator captures each thought to the store and
delegates deeper work to specialists. For fast, offline, no-API management of
your plate (list / add / done / agenda), use the CLI instead:

    python -m jarvis agenda

Usage:
    python main.py                       # prompts you for a dump
    python main.py "start a club, ..."   # non-interactive: dump passed as args
"""

import asyncio
import sys

from dotenv import load_dotenv
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from jarvis.dumps import DumpLog
from jarvis.ledger import CostLedger
from jarvis.orchestrator import build_orchestrator_options
from jarvis.tools import SERVER_NAME

load_dotenv()

# The model's replies can contain any Unicode (em-dashes, arrows, emoji). The
# Windows console defaults to a legacy codepage that mangles them, so force
# UTF-8 output where the stream supports it.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _describe_tool_use(block: ToolUseBlock) -> str:
    """Human-readable one-liner for a tool call, so the run reads like a log."""
    if block.name == "Task":
        return f"[Delegating to: {block.input.get('subagent_type', '?')}]"
    prefix = f"mcp__{SERVER_NAME}__"
    if block.name.startswith(prefix):
        return f"[store: {block.name[len(prefix):]}]"
    return f"[tool: {block.name}]"


def _get_prompt() -> str:
    """Dump from command-line args if given, otherwise prompt interactively."""
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:])
    return input("What's on your mind? ")


async def main() -> None:
    prompt = _get_prompt()

    # Record the dump verbatim *before* the model runs, so your own words are
    # never lost to a crash, an interrupt, or an API failure.
    dump = DumpLog().append(prompt, source="llm")
    print(f"[dump {dump.id} saved]")

    options = build_orchestrator_options(dump_id=dump.id)

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)
                elif isinstance(block, ToolUseBlock):
                    print(_describe_tool_use(block))
        elif isinstance(message, ResultMessage):
            cost = getattr(message, "total_cost_usd", None)
            if cost is not None:
                ledger = CostLedger()
                try:
                    ledger.append(
                        cost,
                        turns=message.num_turns,
                        duration_ms=message.duration_ms,
                        dump_id=dump.id,
                    )
                    print(f"\n[run cost: ${cost:.4f} | month so far: ${ledger.month_total():.2f}]")
                except OSError:
                    print(f"\n[run cost: ${cost:.4f}]")


if __name__ == "__main__":
    asyncio.run(main())
