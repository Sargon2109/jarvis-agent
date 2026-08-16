"""Tests for models: due-date validation and rendering. No API calls."""

from datetime import date

from jarvis.models import Item, format_item, parse_due


def test_parse_due_accepts_valid_and_strips():
    assert parse_due("2026-08-09") == "2026-08-09"
    assert parse_due("  2026-08-09  ") == "2026-08-09"


def test_parse_due_empty_becomes_none():
    assert parse_due(None) is None
    assert parse_due("") is None
    assert parse_due("   ") is None


def test_parse_due_rejects_bad_dates():
    for bad in ("2026-13-01", "not-a-date", "08/09/2026", "2026-08-32"):
        raised = False
        try:
            parse_due(bad)
        except ValueError:
            raised = True
        assert raised, f"{bad!r} should raise ValueError"


def test_item_create_rejects_bad_due():
    raised = False
    try:
        Item.create("x", due="nope")
    except ValueError:
        raised = True
    assert raised


def test_due_date_returns_date_or_none():
    assert Item.create("x").due_date() is None
    reminder = Item.create("x", kind="reminder", due="2026-08-09")
    assert reminder.due_date() == date(2026, 8, 9)


def test_format_item_includes_id_title_and_kind_domain():
    item = Item.create("start a club", kind="project", domain="club")
    line = format_item(item)
    assert item.id in line
    assert "start a club" in line
    assert "project/club" in line
