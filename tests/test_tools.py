"""Tests for the SDK tool handlers. Exercises the handlers directly — no API calls."""

import asyncio
import tempfile
from pathlib import Path

from jarvis.storage import JSONStore
from jarvis.tools import build_store_tools, build_store_server, store_tool_names


def _fresh_store() -> JSONStore:
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-test-"))
    return JSONStore(tmpdir / "plate.json")


def _handlers(store):
    """Map tool name -> async handler, bound to the given store."""
    return {t.name: t.handler for t in build_store_tools(store)}


def _run(coro):
    return asyncio.run(coro)


def _result_text(result: dict) -> str:
    return result["content"][0]["text"]


# --- capture -----------------------------------------------------------------

def test_capture_thought_persists_and_reports():
    store = _fresh_store()
    h = _handlers(store)
    result = _run(h["capture_thought"]({"raw": "start a CS club", "kind": "project", "domain": "club"}))
    assert not result.get("is_error")
    assert "Captured" in _result_text(result)
    items = store.all()
    assert len(items) == 1 and items[0].kind == "project" and items[0].domain == "club"


def test_capture_thought_missing_raw_is_error():
    store = _fresh_store()
    h = _handlers(store)
    result = _run(h["capture_thought"]({"kind": "task"}))  # no 'raw'
    assert result.get("is_error") is True
    assert store.all() == []


def test_capture_thought_bad_kind_is_error():
    store = _fresh_store()
    h = _handlers(store)
    result = _run(h["capture_thought"]({"raw": "x", "kind": "bogus"}))
    assert result.get("is_error") is True
    assert store.all() == []


def test_capture_thought_bad_due_is_error():
    store = _fresh_store()
    h = _handlers(store)
    result = _run(h["capture_thought"]({"raw": "x", "kind": "reminder", "due": "not-a-date"}))
    assert result.get("is_error") is True
    assert store.all() == []


# --- list --------------------------------------------------------------------

def test_list_plate_empty_and_populated():
    store = _fresh_store()
    h = _handlers(store)
    empty = _run(h["list_plate"]({}))
    assert "Nothing on your plate" in _result_text(empty)

    _run(h["capture_thought"]({"raw": "grind leetcode", "kind": "task", "domain": "leetcode"}))
    populated = _run(h["list_plate"]({}))
    assert "grind leetcode" in _result_text(populated)


def test_list_plate_hides_done_by_default_but_status_filter_shows_it():
    store = _fresh_store()
    h = _handlers(store)
    _run(h["capture_thought"]({"raw": "task one", "kind": "task"}))
    store.complete(store.all()[0].id)

    default_view = _run(h["list_plate"]({}))
    assert "task one" not in _result_text(default_view)

    done_view = _run(h["list_plate"]({"status": "done"}))
    assert "task one" in _result_text(done_view)


def test_list_plate_filters_by_domain():
    store = _fresh_store()
    h = _handlers(store)
    _run(h["capture_thought"]({"raw": "club thing", "kind": "task", "domain": "club"}))
    _run(h["capture_thought"]({"raw": "startup thing", "kind": "task", "domain": "startup"}))
    result = _run(h["list_plate"]({"domain": "club"}))
    text = _result_text(result)
    assert "club thing" in text and "startup thing" not in text


# --- agenda ------------------------------------------------------------------

def test_agenda_tool_surfaces_overdue():
    store = _fresh_store()
    h = _handlers(store)
    _run(h["add_reminder"]({"raw": "book club room", "due": "2000-01-01"}))  # overdue
    result = _run(h["agenda"]({}))
    text = _result_text(result)
    assert "OVERDUE" in text and "book club room" in text


def test_agenda_tool_empty_is_friendly():
    store = _fresh_store()
    h = _handlers(store)
    result = _run(h["agenda"]({}))
    assert "clear" in _result_text(result).lower()


# --- reminder ----------------------------------------------------------------

def test_add_reminder_sets_kind_and_due():
    store = _fresh_store()
    h = _handlers(store)
    result = _run(h["add_reminder"]({"raw": "book club room", "due": "2026-08-09"}))
    assert not result.get("is_error")
    item = store.all()[0]
    assert item.kind == "reminder" and item.due == "2026-08-09"


def test_add_reminder_bad_due_is_error():
    store = _fresh_store()
    h = _handlers(store)
    result = _run(h["add_reminder"]({"raw": "x", "due": "soon"}))
    assert result.get("is_error") is True
    assert store.all() == []


# --- complete ----------------------------------------------------------------

def test_complete_item_marks_done_and_handles_unknown():
    store = _fresh_store()
    h = _handlers(store)
    _run(h["capture_thought"]({"raw": "x", "kind": "task"}))
    item_id = store.all()[0].id

    ok = _run(h["complete_item"]({"id": item_id}))
    assert not ok.get("is_error")
    assert store.get(item_id).status == "done"

    missing = _run(h["complete_item"]({"id": "deadbeef"}))
    assert missing.get("is_error") is True


# --- wiring ------------------------------------------------------------------

def test_captured_items_carry_the_dump_id():
    """Everything captured during a dump-driven run traces back to that dump."""
    store = _fresh_store()
    handlers = {t.name: t.handler for t in build_store_tools(store, dump_id="d1234567")}
    _run(handlers["capture_thought"]({"raw": "start a club", "kind": "project"}))
    _run(handlers["add_reminder"]({"raw": "book room", "due": "2026-08-14"}))
    assert [i.dump_id for i in store.all()] == ["d1234567", "d1234567"]


def test_without_a_dump_id_items_are_unlinked():
    store = _fresh_store()
    h = _handlers(store)
    _run(h["capture_thought"]({"raw": "typed directly", "kind": "task"}))
    assert store.all()[0].dump_id is None


def test_tool_names_are_namespaced_in_order():
    assert store_tool_names() == [
        "mcp__jarvis_store__capture_thought",
        "mcp__jarvis_store__list_plate",
        "mcp__jarvis_store__agenda",
        "mcp__jarvis_store__add_reminder",
        "mcp__jarvis_store__complete_item",
    ]


def test_build_store_server_produces_sdk_config():
    config = build_store_server(_fresh_store())
    server_type = getattr(config, "type", None) or (config.get("type") if isinstance(config, dict) else None)
    assert server_type == "sdk"
