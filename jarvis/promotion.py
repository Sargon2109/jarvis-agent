"""Promotion — deciding when a recurring area has earned its own specialist.

The generalist catches everything no specialist owns, which guarantees nothing is
dropped. But a domain the generalist keeps handling is evidence: this is a real,
recurring part of the user's life, and it deserves an expert.

The deliberate choice here is **promotion, not auto-creation**. Spawning an agent
the first time a new word appears produces dozens of one-off specialists with
thin, auto-written prompts — which makes routing *worse*, because the orchestrator
then has to choose between many vague descriptions. Requiring a domain to recur
before it can be promoted means agents are earned, and there are few enough of
them to stay sharp.

This module is pure logic over items — no storage, no SDK, no model.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Optional

from .models import Item

#: How many times a domain must appear before it's worth its own specialist.
DEFAULT_THRESHOLD = 3

#: Labels that are catch-alls rather than real areas — never promote these.
GENERIC_DOMAINS: frozenset[str] = frozenset({"", "other", "misc", "general", "unknown"})

#: How many example titles to carry along as evidence.
SAMPLE_SIZE = 3


@dataclass
class PromotionCandidate:
    """A recurring domain with no specialist — a proposed new agent."""

    domain: str
    count: int
    titles: list[str]       # a few real examples, as evidence
    first_seen: str         # ISO timestamp of the earliest item
    last_seen: str          # ISO timestamp of the most recent item

    def summary(self) -> str:
        examples = "; ".join(self.titles)
        return f"{self.domain} ({self.count} items) - e.g. {examples}"


def find_candidates(
    items: Iterable[Item],
    covered_domains: Iterable[str],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    ignore: frozenset[str] = GENERIC_DOMAINS,
) -> list[PromotionCandidate]:
    """Domains that recur at least ``threshold`` times and have no specialist.

    Ordered by how often they came up (most first), then alphabetically so the
    output is stable between runs.
    """
    covered = {d.strip().lower() for d in covered_domains}
    grouped: dict[str, list[Item]] = defaultdict(list)

    for item in items:
        domain = (item.domain or "").strip().lower()
        if not domain or domain in ignore or domain in covered:
            continue
        grouped[domain].append(item)

    candidates: list[PromotionCandidate] = []
    for domain, group in grouped.items():
        if len(group) < threshold:
            continue
        ordered = sorted(group, key=lambda i: i.created_at)
        candidates.append(
            PromotionCandidate(
                domain=domain,
                count=len(group),
                titles=[i.title for i in ordered[-SAMPLE_SIZE:]],
                first_seen=ordered[0].created_at,
                last_seen=ordered[-1].created_at,
            )
        )

    return sorted(candidates, key=lambda c: (-c.count, c.domain))


def find_candidate(
    items: Iterable[Item],
    covered_domains: Iterable[str],
    domain: str,
    *,
    threshold: int = DEFAULT_THRESHOLD,
) -> Optional[PromotionCandidate]:
    """The candidate for one specific domain, or None if it hasn't earned it."""
    target = domain.strip().lower()
    for candidate in find_candidates(items, covered_domains, threshold=threshold):
        if candidate.domain == target:
            return candidate
    return None


# --- drafting the new agent --------------------------------------------------

def draft_description(domain: str) -> str:
    """The 'when to use me' line the orchestrator routes on."""
    return (
        f"Work in the {domain} area. Use for any item whose domain is '{domain}'."
    )


def draft_prompt(domain: str, titles: Iterable[str]) -> str:
    """A starting prompt for a promoted agent, grounded in real captured work.

    An auto-written prompt is weaker than a hand-written one, so this does two
    things to compensate: it quotes the user's actual items so the agent has real
    context rather than a bare label, and it says plainly that it's a draft meant
    to be sharpened.
    """
    examples = "\n".join(f"  - {title}" for title in titles)
    return (
        f"You are Jarvis's {domain} specialist.\n\n"
        f"This area kept coming up in the user's own words. Real examples of what "
        f"they've asked for here:\n{examples}\n\n"
        "Work from what the user actually asked for, and produce concrete, finished "
        "artifacts rather than plans to produce them later. Where you don't yet know "
        "what 'done' looks like in this area, ask instead of guessing.\n\n"
        "This prompt was drafted automatically when the area proved recurring. It is "
        "a starting point — sharpen it as it becomes clear what genuinely good work "
        "in this domain looks like."
    )


def render_candidates(candidates: list[PromotionCandidate]) -> str:
    """Human-readable rendering for the CLI and the briefing."""
    if not candidates:
        return "No areas have recurred often enough to need their own specialist yet."
    lines = []
    for candidate in candidates:
        lines.append(f"  {candidate.domain} - came up {candidate.count} times")
        for title in candidate.titles:
            lines.append(f"      {title}")
    return "\n".join(lines)
