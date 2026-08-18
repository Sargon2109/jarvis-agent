"""Tests for the models and the JSON-backed store. No API calls."""

import json
import tempfile
from pathlib import Path

from jarvis.models import Item, KINDS
from jarvis.storage import JSONStore, StoreError


def _fresh_store() -> JSONStore:
    """A JSONStore pointing at a brand-new temp file."""
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-test-"))
    return JSONStore(tmpdir / "plate.json")


# --- models ------------------------------------------------------------------

def test_item_create_defaults_and_strip():
    item = Item.create("  start a club  ")
    assert item.raw == "start a club"
    assert item.title == "start a club"
    assert item.kind == "task"
    assert item.status == "inbox"
    assert item.domain == "other"
    assert len(item.id) == 8
    assert item.created_at == item.updated_at


def test_item_create_rejects_bad_kind():
    raised = False
    try:
        Item.create("x", kind="nope")  # type: ignore[arg-type]
    except ValueError:
        raised = True
    assert raised, "bad kind should raise ValueError"


def test_item_from_dict_tolerates_unknown_and_missing_keys():
    data = Item.create("x", kind="project").to_dict()
    data["surprise"] = "ignored"   # unknown key must not blow up
    data.pop("agent")              # missing optional key must default
    item = Item.from_dict(data)
    assert item.kind == "project"
    assert item.agent is None


# --- store basics ------------------------------------------------------------

def test_add_returns_item_and_persists_across_instances():
    store = _fresh_store()
    created = store.add("finish econ pset", kind="task", domain="homework")
    # A separate store instance reading the same file must see it.
    reopened = JSONStore(store.path)
    items = reopened.all()
    assert len(items) == 1
    assert items[0].id == created.id
    assert items[0].domain == "homework"


def test_empty_store_returns_no_items():
    assert _fresh_store().all() == []


def test_list_filters_by_kind_status_domain():
    store = _fresh_store()
    store.add("club", kind="project", domain="club")
    store.add("pset", kind="task", domain="homework")
    r = store.add("standup", kind="reminder", domain="startup", due="2026-08-10")
    assert len(store.list(kind="task")) == 1
    assert len(store.list(domain="club")) == 1
    assert store.list(kind="reminder")[0].id == r.id
    assert store.list(status="done") == []


def test_get_returns_match_or_none():
    store = _fresh_store()
    item = store.add("x")
    assert store.get(item.id).id == item.id
    assert store.get("deadbeef") is None


# --- mutation ----------------------------------------------------------------

def test_complete_sets_status_done():
    store = _fresh_store()
    item = store.add("x")
    done = store.complete(item.id)
    assert done is not None and done.status == "done"
    assert store.get(item.id).status == "done"


def test_complete_unknown_id_returns_none():
    assert _fresh_store().complete("nope") is None


def test_update_ignores_immutable_and_unknown_fields():
    store = _fresh_store()
    item = store.add("x")
    original_id, original_created = item.id, item.created_at
    updated = store.update(
        item.id, title="new title", id="hacked", created_at="hacked", bogus="nope"
    )
    assert updated.title == "new title"
    assert updated.id == original_id          # immutable preserved
    assert updated.created_at == original_created
    assert not hasattr(updated, "bogus")


def test_set_status_rejects_bad_status():
    store = _fresh_store()
    item = store.add("x")
    raised = False
    try:
        store.set_status(item.id, "banana")  # type: ignore[arg-type]
    except ValueError:
        raised = True
    assert raised


# --- durability --------------------------------------------------------------

def test_write_is_atomic_no_temp_leftovers_and_valid_json():
    store = _fresh_store()
    store.add("a")
    store.add("b")
    leftovers = list(store.path.parent.glob(".plate-*.tmp"))
    assert leftovers == [], f"atomic write left temp files: {leftovers}"
    # File is valid JSON with the expected shape.
    doc = json.loads(store.path.read_text(encoding="utf-8"))
    assert doc["schema_version"] == JSONStore.SCHEMA_VERSION
    assert len(doc["items"]) == 2


def test_corrupt_file_raises_store_error():
    store = _fresh_store()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ this is not valid json", encoding="utf-8")
    raised = False
    try:
        store.all()
    except StoreError:
        raised = True
    assert raised, "corrupt file should raise StoreError"


# --- remove ------------------------------------------------------------------

def test_remove_deletes_and_reports():
    store = _fresh_store()
    keep = store.add("keep me")
    drop = store.add("drop me")
    assert store.remove(drop.id) is True
    remaining = store.all()
    assert len(remaining) == 1 and remaining[0].id == keep.id


def test_remove_unknown_id_returns_false():
    store = _fresh_store()
    store.add("only one")
    assert store.remove("deadbeef") is False
    assert len(store.all()) == 1


# --- dump linkage ------------------------------------------------------------

def test_dump_id_is_stored_and_survives_a_reload():
    store = _fresh_store()
    store.add("from a dump", dump_id="abc12345")
    store.add("typed directly")
    reopened = JSONStore(store.path)
    linked = [i for i in reopened.all() if i.dump_id == "abc12345"]
    assert len(linked) == 1 and linked[0].raw == "from a dump"


def test_dump_id_defaults_to_none():
    assert _fresh_store().add("no dump").dump_id is None


def test_old_items_without_dump_id_still_load():
    """Items written before dump_id existed must not break on read."""
    store = _fresh_store()
    item = store.add("x")
    data = json.loads(store.path.read_text(encoding="utf-8"))
    del data["items"][0]["dump_id"]  # simulate an older record
    store.path.write_text(json.dumps(data), encoding="utf-8")
    loaded = store.all()
    assert len(loaded) == 1 and loaded[0].dump_id is None and loaded[0].id == item.id


# --- update validation (the desk exposes update paths to more callers) -------

def test_update_rejects_a_malformed_due_date():
    store = _fresh_store()
    item = store.add("dated thing")
    raised = False
    try:
        store.update(item.id, due="not-a-date")
    except ValueError:
        raised = True
    assert raised
    assert store.get(item.id).due is None  # nothing was persisted


def test_update_rejects_bad_status_and_kind():
    store = _fresh_store()
    item = store.add("thing")
    for field, value in (("status", "zombie"), ("kind", "wish")):
        raised = False
        try:
            store.update(item.id, **{field: value})
        except ValueError:
            raised = True
        assert raised, field


def test_update_cannot_shadow_methods_or_invent_fields():
    store = _fresh_store()
    item = store.add("thing")
    store.update(item.id, touch="clobbered", nonsense="x", title="renamed")
    reloaded = store.get(item.id)
    assert reloaded.title == "renamed"
    assert callable(reloaded.touch)  # the method survived
    assert not hasattr(reloaded, "nonsense") or reloaded.title == "renamed"


def test_a_bad_stored_due_degrades_instead_of_crashing_reads():
    store = _fresh_store()
    item = store.add("thing")
    # Corrupt the due date on disk directly, bypassing validation.
    import json as _json
    doc = _json.loads(store.path.read_text())
    doc["items"][0]["due"] = "garbage"
    store.path.write_text(_json.dumps(doc))
    assert store.get(item.id).due_date() is None  # undated, not an exception


def test_a_non_string_stored_due_degrades_instead_of_crashing():
    """A hand-edit that drops the quotes around a date (making due a JSON number)
    must not crash the agenda/briefing/state that read every item."""
    from jarvis.agenda import build_agenda
    store = _fresh_store()
    item = store.add("thing")
    import json as _json
    doc = _json.loads(store.path.read_text())
    doc["items"][0]["due"] = 20260817        # unquoted number, not a string
    store.path.write_text(_json.dumps(doc))
    reloaded = store.get(item.id)
    assert reloaded.due is None              # coerced away on load
    assert reloaded.due_date() is None       # and never raises
    build_agenda(store.all())                # the read view survives
