"""Tests for desktop notifications. Fully offline — the sender is injected."""

import tempfile
from datetime import date
from pathlib import Path

from jarvis.models import Item
from jarvis.notify import (
    Notification,
    NotifyState,
    _osascript_args,
    build_notifications,
    run_notify,
)

TODAY = date(2026, 8, 21)


def _state() -> NotifyState:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-notify-"))
    return NotifyState(tmp / "state.json")


def _item(title, due, status="inbox"):
    item = Item.create(title, due=due)
    item.status = status
    return item


# --- selection ---------------------------------------------------------------

def test_only_overdue_and_today_notify():
    items = [
        _item("late thing", "2026-08-19"),
        _item("today thing", "2026-08-21"),
        _item("future thing", "2026-09-01"),     # upcoming: never an interruption
        _item("done thing", "2026-08-19", status="done"),
        Item.create("undated thing"),
    ]
    notifications = build_notifications(items, today=TODAY)
    assert [n.body for n in notifications] == ["late thing", "today thing"]
    assert notifications[0].title == "Overdue (2026-08-19)"
    assert notifications[1].title == "Due today"


# --- dedupe ------------------------------------------------------------------

def test_each_item_notifies_once_per_day():
    state = _state()
    items = [_item("thing", "2026-08-21")]

    first = run_notify(items, state=state, sender=lambda n: True, today=TODAY)
    assert len(first.sent) == 1

    second = run_notify(items, state=state, sender=lambda n: True, today=TODAY)
    assert second.sent == [] and second.deduped == 1

    # A new day resets the slate.
    tomorrow = date(2026, 8, 22)
    items[0].due = "2026-08-22"
    third = run_notify(items, state=state, sender=lambda n: True, today=tomorrow)
    assert len(third.sent) == 1


def test_dry_run_sends_and_records_nothing():
    state = _state()
    calls = []
    result = run_notify(
        [_item("thing", "2026-08-21")],
        state=state, sender=lambda n: calls.append(n) or True,
        today=TODAY, dry_run=True,
    )
    assert len(result.sent) == 1     # reported as would-send
    assert calls == []               # nothing actually sent
    assert state.sent_today(TODAY) == set()   # nothing recorded


def test_failed_sends_are_not_marked_as_announced():
    state = _state()
    items = [_item("thing", "2026-08-21")]
    result = run_notify(items, state=state, sender=lambda n: False, today=TODAY)
    assert result.failed == 1 and result.sent == []
    # Not recorded, so the next run retries instead of going silent forever.
    retry = run_notify(items, state=state, sender=lambda n: True, today=TODAY)
    assert len(retry.sent) == 1


def test_corrupt_state_file_degrades_to_notifying_again():
    state = _state()
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text("{not json", encoding="utf-8")
    assert state.sent_today(TODAY) == set()


# --- the macOS sender's command ----------------------------------------------

def test_osascript_escapes_quotes_and_backslashes():
    args = _osascript_args(
        Notification(item_id="x", title='Say "hi"', body='back\\slash "quote"')
    )
    assert args[0] == "osascript"
    script = args[2]
    assert '\\"hi\\"' in script
    assert "back\\\\slash" in script
    # The AppleScript string can't be broken out of by the item title.
    assert script.count('"') % 2 == 0


def test_a_nul_byte_in_a_title_fails_the_send_instead_of_crashing():
    """subprocess rejects a NUL argument with ValueError; one unsendable
    notification must not take down a scheduled run."""
    from jarvis.notify import _default_sender
    assert _default_sender(Notification("i", "T", "bad" + chr(0) + "title")) is False
