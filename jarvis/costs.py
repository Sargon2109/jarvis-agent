"""``jarvis costs`` — where the money actually went.

A monthly total tells you that Jarvis is expensive; it doesn't tell you why,
and the two useful answers look nothing alike. A run is priced almost entirely
on what the model had to *read*, which comes from three places:

* **A fixed preamble.** Roughly 27,000 tokens accompany every single message —
  the CLI's built-in tool schemas, Jarvis's system prompt, and the definitions
  of every specialist. This is a floor, not a variable: trimming the granted
  tool list does not shrink it, because the allowlist governs permission, not
  what gets sent.
* **Each delegation.** A specialist starts with its own copy of that preamble,
  so a delegating run pays it again per agent — measured at roughly 7x a plain
  reply for a single delegation.
* **Everything already said.** Conversation history and any document text
  pulled in are re-read on every later turn. A slide deck read once is paid
  for, at the cache rate, for the rest of that conversation.

So this report leads with cost per run, and shows the context size and
delegation count beside it — the two numbers that explain the bill.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .dumps import DumpLog
from .ledger import CostEntry, CostLedger

#: What one message costs before doing anything, measured on this project.
BASELINE_CONTEXT_TOKENS = 27_000


@dataclass
class RunCost:
    """One run, after collapsing duplicate ledger rows."""

    entry: CostEntry
    prompt: str

    @property
    def cost(self) -> float:
        return self.entry.cost_usd


def collapse(entries: list[CostEntry]) -> list[CostEntry]:
    """One row per run, newest last.

    Rows sharing a dump_id are snapshots of one run's running total, so the
    largest is that run's real cost. See CostLedger._sum for why they exist.
    """
    largest: dict[str, CostEntry] = {}
    loose: list[CostEntry] = []
    for entry in entries:
        if not entry.dump_id:
            loose.append(entry)
            continue
        seen = largest.get(entry.dump_id)
        if seen is None or entry.cost_usd > seen.cost_usd:
            largest[entry.dump_id] = entry
    return sorted([*largest.values(), *loose], key=lambda e: e.created_at)


def build_report(
    ledger: Optional[CostLedger] = None,
    log: Optional[DumpLog] = None,
    *,
    top: int = 10,
) -> str:
    """The rendered breakdown."""
    ledger = ledger or CostLedger()
    log = log or DumpLog()
    runs = collapse(ledger.all())
    if not runs:
        return "No runs recorded yet — nothing has been spent."

    prompts = {d.id: d.text for d in log.all()}
    total = sum(r.cost_usd for r in runs)
    lines = [
        f"{len(runs)} runs, ${total:.2f} total, ${total / len(runs):.3f} average.",
        "",
    ]

    # --- what the money bought ----------------------------------------------
    delegating = [r for r in runs if r.agents]
    plain = [r for r in runs if not r.agents]
    if delegating and plain:
        avg_d = sum(r.cost_usd for r in delegating) / len(delegating)
        avg_p = sum(r.cost_usd for r in plain) / len(plain)
        lines.append(
            f"Runs that delegated:     {len(delegating):>3}  avg ${avg_d:.3f}"
        )
        lines.append(
            f"Runs that did not:       {len(plain):>3}  avg ${avg_p:.3f}"
        )
        if avg_p > 0:
            lines.append(f"  -> delegating costs about {avg_d / avg_p:.0f}x more per run.")
        lines.append("")

    # --- the expensive ones --------------------------------------------------
    ranked = sorted(runs, key=lambda e: e.cost_usd, reverse=True)[:top]
    covered = sum(r.cost_usd for r in ranked)
    lines.append(f"Most expensive runs ({covered / total * 100:.0f}% of all spend):")
    for entry in ranked:
        prompt = (prompts.get(entry.dump_id or "") or "(no prompt recorded)")
        prompt = " ".join(prompt.split())[:60]
        context = entry.context_tokens()
        detail = f"{context:>9,} tok" if context else "    (no token data)"
        agents = f"{entry.agents} agents" if entry.agents else "no delegation"
        lines.append(
            f"  ${entry.cost_usd:>6.3f}  {detail}  {agents:<13}  {prompt}"
        )

    # --- the floor -----------------------------------------------------------
    measured = [r for r in runs if r.context_tokens()]
    lines.append("")
    lines.append(
        f"Every message carries ~{BASELINE_CONTEXT_TOKENS:,} tokens of tool schemas "
        "and agent\ndefinitions before it does any work. That is a floor, not a "
        "setting."
    )
    if measured:
        cheapest = min(r.context_tokens() for r in measured)
        lines.append(f"Smallest context actually recorded: {cheapest:,} tokens.")
    lines.append("")
    lines.append("To spend less: ask for one course rather than all of them, start a")
    lines.append("new session when a conversation gets long, and prefer the syllabus")
    lines.append("or outline over reading whole slide decks.")
    return "\n".join(lines)
