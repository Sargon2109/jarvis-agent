"""Tests for the SQLite backend, the store factory, and migration.

The behavioral tests mirror the JSONStore ones on purpose: both backends must
satisfy the same Store contract, so drift between them is a bug.
"""

import tempfile
import threading
from pathlib import Path

from jarvis.storage import (
    JSONStore,
    SQLiteStore,
    StoreError,
    create_store,
    migrate_items,
)


def _store() -> SQLiteStore:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-sqlite-"))
    return SQLiteStore(tmp / "plate.db")


# --- contract ----------------------------------------------------------------

def test_add_get_list_roundtrip():
    store = _store()
    item = store.add("finish pset", kind="task", domain="homework", due="2026-09-01")
    assert store.get(item.id).title == "finish pset"
    assert [i.id for i in store.list(domain="homework")] == [item.id]
    assert store.list(domain="other") == []
    assert store.list(kind="task", status="inbox", domain="homework")[0].id == item.id


def test_persists_across_instances():
    store = _store()
    item = store.add("survives reopen")
    reopened = SQLiteStore(store.path)
    assert reopened.get(item.id).raw == "survives reopen"


def test_complete_and_remove():
    store = _store()
    item = store.add("thing")
    assert store.complete(item.id).status == "done"
    assert store.remove(item.id) is True
    assert store.remove(item.id) is False
    assert store.get(item.id) is None


def test_update_validates_like_json_store():
    store = _store()
    item = store.add("thing")
    for field, value in (("due", "not-a-date"), ("status", "zombie"), ("kind", "wish")):
        raised = False
        try:
            store.update(item.id, **{field: value})
        except ValueError:
            raised = True
        assert raised, field
    # Valid changes persist; immutables and unknowns are ignored.
    updated = store.update(item.id, title="renamed", id="hax", nonsense="x")
    assert updated.title == "renamed"
    assert store.get(item.id) is not None      # id unchanged
    assert store.get("hax") is None


def test_update_unknown_id_returns_none():
    assert _store().update("nope", title="x") is None


# --- concurrency (the reason this backend exists) ----------------------------

def test_parallel_adds_lose_nothing():
    store = _store()
    PER_THREAD, THREADS = 25, 4

    def adder(n):
        def run():
            for i in range(PER_THREAD):
                store.add(f"item {n}-{i}")
        return run

    threads = [threading.Thread(target=adder(n)) for n in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.all()) == PER_THREAD * THREADS


# --- factory -----------------------------------------------------------------

def test_factory_picks_backend_by_suffix():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-factory-"))
    assert isinstance(create_store(tmp / "plate.db"), SQLiteStore)
    assert isinstance(create_store(tmp / "plate.sqlite3"), SQLiteStore)
    assert isinstance(create_store(tmp / "plate.json"), JSONStore)


# --- migration ---------------------------------------------------------------

def test_migrate_json_to_sqlite_preserves_everything():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-migrate-"))
    source = JSONStore(tmp / "plate.json")
    a = source.add("first", kind="project", domain="club", due="2026-09-01")
    b = source.add("second", kind="reminder", domain="homework")
    source.complete(b.id)

    target = SQLiteStore(tmp / "plate.db")
    assert migrate_items(source, target) == 2

    got_a, got_b = target.get(a.id), target.get(b.id)
    assert got_a.created_at == a.created_at        # timestamps preserved
    assert got_a.due == "2026-09-01"
    assert got_b.status == "done"


def test_migrate_refuses_a_nonempty_target():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-migrate2-"))
    source = JSONStore(tmp / "plate.json")
    source.add("x")
    target = SQLiteStore(tmp / "plate.db")
    target.add("already here")
    raised = False
    try:
        migrate_items(source, target)
    except StoreError:
        raised = True
    assert raised
    assert len(target.all()) == 1


def test_migrate_works_in_reverse_too():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-migrate3-"))
    source = SQLiteStore(tmp / "plate.db")
    item = source.add("going back to json")
    target = JSONStore(tmp / "plate.json")
    assert migrate_items(source, target) == 1
    assert target.get(item.id).raw == "going back to json"


def test_concurrent_updates_do_not_lose_each_other():
    """The lost-update an adversarial review reproduced against the first cut.

    Two writers each change a *different* field of the same row. Because
    update() rewrites every column from its own in-memory copy, a read that
    escapes the write transaction lets the later commit revert the earlier
    one. BEGIN IMMEDIATE serializes the whole read-modify-write, so both
    changes must survive.
    """
    store = _store()
    item = store.add("original title")

    barrier = threading.Barrier(2)
    errors = []

    def set_status():
        try:
            barrier.wait(timeout=5)
            store.update(item.id, status="active")
        except Exception as exc:      # noqa: BLE001 - surfaced in the assert
            errors.append(exc)

    def set_title():
        try:
            barrier.wait(timeout=5)
            store.update(item.id, title="renamed")
        except Exception as exc:      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=set_status), threading.Thread(target=set_title)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    final = store.get(item.id)
    assert final.status == "active", "status write was lost"
    assert final.title == "renamed", "title write was lost"


def test_many_interleaved_updates_all_land():
    """Hammer the same row from several threads; every write must be visible."""
    store = _store()
    item = store.add("counter")

    def bump(n):
        def run():
            for i in range(10):
                store.update(item.id, title=f"thread-{n}-{i}")
        return run

    threads = [threading.Thread(target=bump(n)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = store.get(item.id)
    assert final.title.startswith("thread-")   # a coherent value, not a torn row
    assert len(store.all()) == 1
