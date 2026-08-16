"""Turning dated items into an agenda: what's overdue, due today, and upcoming.

This is the read-side that makes reminders useful — the piece that lets Jarvis
(or the CLI) answer "what's on my plate *right now*?" It's pure logic over a
list of :class:`~jarvis.models.Item`, with no storage or SDK dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .models import Item, format_item


@dataclass
class Agenda:
    """Dated, not-yet-done items bucketed by urgency (each list sorted by due date)."""

    overdue: list[Item]
    today: list[Item]
    upcoming: list[Item]

    def is_empty(self) -> bool:
        return not (self.overdue or self.today or self.upcoming)


def build_agenda(items: Iterable[Item], *, today: date | None = None) -> Agenda:
    """Bucket dated, unfinished items into overdue / today / upcoming.

    Items that are done or have no due date are excluded — an agenda is about
    what still needs attention on a timeline.
    """
    today = today or date.today()
    overdue: list[Item] = []
    due_today: list[Item] = []
    upcoming: list[Item] = []

    for item in items:
        if item.status == "done":
            continue
        due = item.due_date()
        if due is None:
            continue
        if due < today:
            overdue.append(item)
        elif due == today:
            due_today.append(item)
        else:
            upcoming.append(item)

    by_due = lambda item: item.due  # noqa: E731 - trivial key
    return Agenda(
        overdue=sorted(overdue, key=by_due),
        today=sorted(due_today, key=by_due),
        upcoming=sorted(upcoming, key=by_due),
    )


def render_agenda(agenda: Agenda) -> str:
    """Human-readable multi-line rendering of an agenda."""
    if agenda.is_empty():
        return "Nothing scheduled. Your agenda is clear."

    sections = (
        ("OVERDUE", agenda.overdue),
        ("TODAY", agenda.today),
        ("UPCOMING", agenda.upcoming),
    )
    lines: list[str] = []
    for heading, bucket in sections:
        if not bucket:
            continue
        lines.append(heading)
        lines.extend(f"  {format_item(item)}" for item in bucket)
    return "\n".join(lines)
