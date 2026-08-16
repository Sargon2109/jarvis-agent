"""The briefing — what Jarvis tells you when you haven't asked.

The agenda answers "what's due?". A briefing answers the harder question: *what
would I want to know right now if I hadn't thought to ask?* That's four things,
and only the first is about dates:

  * what's overdue, due today, and coming up;
  * what's been sitting untouched long enough that you've clearly forgotten it;
  * what you dumped but never sorted;
  * which recurring areas have earned their own specialist.

This is the payload the morning automation runs. It's pure logic over data you
already have — no model, no network — so it costs nothing to produce and works
offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from .agenda import Agenda, build_agenda
from .dumps import DumpRecord
from .models import Item, format_item
from .promotion import DEFAULT_THRESHOLD, PromotionCandidate, find_candidates

#: An untouched inbox item older than this is probably forgotten, not pending.
DEFAULT_STALE_DAYS = 14

#: Cap on how many stale items to surface — a briefing you skim is worthless.
MAX_STALE = 5


@dataclass
class Briefing:
    """Everything worth knowing right now, assembled from local data only.

    ``now`` is the single clock the whole briefing is measured against, kept so
    that rendering reports the same ages the selection used — a briefing is a
    snapshot of one moment, not a mix of two.
    """

    agenda: Agenda
    stale: list[Item] = field(default_factory=list)
    unsorted_dumps: list[DumpRecord] = field(default_factory=list)
    candidates: list[PromotionCandidate] = field(default_factory=list)
    active_count: int = 0
    now: Optional[datetime] = None

    def is_quiet(self) -> bool:
        """True when there is genuinely nothing to report."""
        return (
            self.agenda.is_empty()
            and not self.stale
            and not self.unsorted_dumps
            and not self.candidates
        )


def find_stale(
    items: Iterable[Item],
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    now: Optional[datetime] = None,
) -> list[Item]:
    """Unfinished items nobody has touched in a while, oldest first.

    Items with a due date are excluded — those already surface through the
    agenda, and saying the same thing twice makes a briefing easy to ignore.
    """
    now = now or datetime.now(timezone.utc)
    stale = [
        item
        for item in items
        if item.status != "done"
        and not item.due
        and (item.age_days(now) or 0) >= stale_days
    ]
    return sorted(stale, key=lambda i: i.updated_at)[:MAX_STALE]


def find_unsorted_dumps(
    dumps: Iterable[DumpRecord], items: Iterable[Item]
) -> list[DumpRecord]:
    """Dumps that never became items — captured, but never processed."""
    sorted_ids = {item.dump_id for item in items if item.dump_id}
    return [record for record in dumps if record.id not in sorted_ids]


def build_briefing(
    items: Iterable[Item],
    dumps: Iterable[DumpRecord] = (),
    covered_domains: Iterable[str] = (),
    *,
    today: Optional[date] = None,
    now: Optional[datetime] = None,
    stale_days: int = DEFAULT_STALE_DAYS,
    threshold: int = DEFAULT_THRESHOLD,
) -> Briefing:
    """Assemble the briefing. Pure function — same inputs, same output."""
    items = list(items)
    dumps = list(dumps)
    now = now or datetime.now(timezone.utc)
    return Briefing(
        agenda=build_agenda(items, today=today),
        stale=find_stale(items, stale_days=stale_days, now=now),
        unsorted_dumps=find_unsorted_dumps(dumps, items),
        candidates=find_candidates(items, covered_domains, threshold=threshold),
        active_count=sum(1 for i in items if i.status != "done"),
        now=now,
    )


def render_briefing(briefing: Briefing, *, stale_days: int = DEFAULT_STALE_DAYS) -> str:
    """The briefing as text — what the morning job prints or emails you."""
    if briefing.is_quiet():
        return "Nothing needs you right now. Your plate is clear."

    blocks: list[str] = []
    agenda = briefing.agenda

    for heading, bucket in (
        ("OVERDUE", agenda.overdue),
        ("TODAY", agenda.today),
        ("UPCOMING", agenda.upcoming),
    ):
        if bucket:
            lines = [heading] + [f"  {format_item(item)}" for item in bucket]
            blocks.append("\n".join(lines))

    if briefing.stale:
        lines = [f"SITTING UNTOUCHED ({stale_days}+ days)"]
        for item in briefing.stale:
            age = item.age_days(briefing.now)
            suffix = f"  [{age}d]" if age is not None else ""
            lines.append(f"  {format_item(item)}{suffix}")
        blocks.append("\n".join(lines))

    if briefing.unsorted_dumps:
        lines = ["CAPTURED BUT NOT SORTED"]
        for record in briefing.unsorted_dumps:
            lines.append(f"  [{record.id}] {record.summary(60)}")
        lines.append("  -> run `python main.py` to have Jarvis sort these")
        blocks.append("\n".join(lines))

    if briefing.candidates:
        lines = ["KEEPS COMING UP - no specialist yet"]
        for candidate in briefing.candidates:
            lines.append(f"  {candidate.domain} - {candidate.count} items")
        lines.append("  -> `jarvis promote <domain>` to give it its own agent")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)
