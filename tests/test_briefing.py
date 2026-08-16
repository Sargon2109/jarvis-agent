"""Tests for the briefing — the unprompted 'what should I know?' digest. No API calls."""

from datetime import date, datetime, timedelta, timezone

from jarvis.briefing import (
    build_briefing,
    find_stale,
    find_unsorted_dumps,
    render_briefing,
)
from jarvis.dumps import DumpRecord
from jarvis.models import Item

NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)
TODAY = date(2026, 8, 11)


def _aged(title: str, days: int, **kw) -> Item:
    item = Item.create(title, **kw)
    stamp = (NOW - timedelta(days=days)).isoformat(timespec="seconds")
    item.created_at = item.updated_at = stamp
    return item


# --- stale detection ---------------------------------------------------------

def test_stale_finds_only_old_untouched_items():
    items = [_aged("forgotten", 30), _aged("fresh", 1)]
    stale = find_stale(items, stale_days=14, now=NOW)
    assert [i.title for i in stale] == ["forgotten"]


def test_done_items_are_never_stale():
    old = _aged("finished long ago", 60)
    old.status = "done"
    assert find_stale([old], stale_days=14, now=NOW) == []


def test_dated_items_are_left_to_the_agenda():
    """An item with a due date already surfaces there; don't say it twice."""
    dated = _aged("has a deadline", 60, kind="reminder", due="2026-09-01")
    assert find_stale([dated], stale_days=14, now=NOW) == []


def test_stale_is_capped_and_oldest_first():
    items = [_aged(f"old {i}", 20 + i) for i in range(9)]
    stale = find_stale(items, stale_days=14, now=NOW)
    assert len(stale) == 5                      # capped
    assert stale[0].title == "old 8"            # oldest first


# --- unsorted dumps ----------------------------------------------------------

def test_unsorted_dumps_are_those_with_no_items():
    sorted_dump = DumpRecord.create("became a task")
    orphan = DumpRecord.create("never processed")
    items = [Item.create("a task", dump_id=sorted_dump.id)]
    unsorted = find_unsorted_dumps([sorted_dump, orphan], items)
    assert [d.id for d in unsorted] == [orphan.id]


# --- assembly ----------------------------------------------------------------

def test_quiet_briefing_when_nothing_needs_attention():
    briefing = build_briefing([], [], [], today=TODAY, now=NOW)
    assert briefing.is_quiet()
    assert "clear" in render_briefing(briefing).lower()


def test_briefing_gathers_all_four_signals():
    items = [
        Item.create("overdue thing", kind="reminder", due="2026-08-01"),
        _aged("forgotten thing", 40),
        *[Item.create(f"debate {i}", domain="debate") for i in range(3)],
    ]
    orphan = DumpRecord.create("never sorted")
    briefing = build_briefing(items, [orphan], ["club"], today=TODAY, now=NOW)

    assert briefing.agenda.overdue                     # dated
    assert any(i.title == "forgotten thing" for i in briefing.stale)
    assert briefing.unsorted_dumps                     # captured, unprocessed
    assert [c.domain for c in briefing.candidates] == ["debate"]
    assert not briefing.is_quiet()


def test_active_count_excludes_done():
    items = [Item.create("a"), Item.create("b")]
    items[0].status = "done"
    assert build_briefing(items, [], [], today=TODAY, now=NOW).active_count == 1


# --- rendering ---------------------------------------------------------------

def test_render_shows_only_sections_that_have_content():
    items = [Item.create("overdue", kind="reminder", due="2026-08-01")]
    text = render_briefing(build_briefing(items, [], [], today=TODAY, now=NOW))
    assert "OVERDUE" in text
    assert "SITTING UNTOUCHED" not in text
    assert "CAPTURED BUT NOT SORTED" not in text


def test_render_includes_next_actions():
    orphan = DumpRecord.create("unsorted thing")
    items = [Item.create(f"debate {i}", domain="debate") for i in range(3)]
    text = render_briefing(build_briefing(items, [orphan], [], today=TODAY, now=NOW))
    assert "main.py" in text          # how to sort the dump
    assert "jarvis promote" in text   # how to act on the suggestion


def test_render_shows_age_on_stale_items():
    text = render_briefing(
        build_briefing([_aged("forgotten", 40)], [], [], today=TODAY, now=NOW)
    )
    assert "[40d]" in text
