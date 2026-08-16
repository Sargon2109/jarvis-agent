"""Tests for the agenda bucketing. No API calls."""

from datetime import date

from jarvis.agenda import build_agenda, render_agenda
from jarvis.models import Item


def _reminder(due, status="inbox"):
    item = Item.create("r", kind="reminder", due=due)
    item.status = status
    return item


def test_buckets_relative_to_today():
    today = date(2026, 8, 9)
    items = [_reminder("2026-08-01"), _reminder("2026-08-09"), _reminder("2026-08-20")]
    agenda = build_agenda(items, today=today)
    assert [i.due for i in agenda.overdue] == ["2026-08-01"]
    assert [i.due for i in agenda.today] == ["2026-08-09"]
    assert [i.due for i in agenda.upcoming] == ["2026-08-20"]


def test_excludes_done_and_undated():
    today = date(2026, 8, 9)
    items = [
        _reminder("2026-08-01", status="done"),   # done -> excluded
        Item.create("no due", kind="task"),        # no due -> excluded
    ]
    assert build_agenda(items, today=today).is_empty()


def test_each_bucket_sorted_by_due():
    today = date(2026, 8, 9)
    items = [_reminder("2026-08-25"), _reminder("2026-08-11"), _reminder("2026-08-18")]
    agenda = build_agenda(items, today=today)
    assert [i.due for i in agenda.upcoming] == ["2026-08-11", "2026-08-18", "2026-08-25"]


def test_render_empty_is_friendly():
    assert "clear" in render_agenda(build_agenda([])).lower()


def test_render_shows_present_headings_only():
    today = date(2026, 8, 9)
    agenda = build_agenda([_reminder("2026-08-01"), _reminder("2026-08-20")], today=today)
    out = render_agenda(agenda)
    assert "OVERDUE" in out
    assert "UPCOMING" in out
    assert "TODAY" not in out   # nothing due today -> heading omitted
