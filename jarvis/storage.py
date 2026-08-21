"""Persistence for Jarvis's memory.

:class:`Store` is the interface every backend implements; :class:`JSONStore` is
the file-backed implementation used today. Keeping the interface separate means
a future SQLite (or cloud) backend can be dropped in without changing the tools
or orchestrator — they depend only on :class:`Store`.

Design choices:
  * The file on disk is the single source of truth — each call reads/writes it,
    so there's never a stale in-memory copy between runs.
  * Writes are **atomic** (temp file + ``os.replace``) so a crash mid-write can
    never corrupt or truncate the user's data.
  * The path can be overridden with the ``JARVIS_STORE_PATH`` env var, so the
    store can be relocated (and tests can point it at a temp file).
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from dataclasses import fields

from .models import Item, KINDS, Kind, STATUSES, Status, parse_due

#: Fallback location: <repo>/data/plate.json (the data/ dir is gitignored).
DEFAULT_STORE_PATH = Path(__file__).resolve().parent.parent / "data" / "plate.json"

#: Env var to override where the store lives.
STORE_PATH_ENV = "JARVIS_STORE_PATH"


def default_store_path() -> Path:
    """Where the store lives: ``$JARVIS_STORE_PATH`` if set, else the default."""
    override = os.environ.get(STORE_PATH_ENV)
    return Path(override) if override else DEFAULT_STORE_PATH


class StoreError(RuntimeError):
    """Raised when the store can't be read (e.g. corrupt or unreadable file)."""


#: Fields a caller may change through update() — real Item fields only, minus
#: the immutable identity pair. Shared by every backend so validation can never
#: drift between them.
UPDATABLE_FIELDS: frozenset[str] = (
    frozenset(f.name for f in fields(Item)) - frozenset({"id", "created_at"})
)


def _validated_change(key: str, value):
    """Normalize/validate one update() change. Raises ValueError on bad values.

    Centralized because a malformed due/status/kind poisons every read view;
    both JSONStore and SQLiteStore funnel through here.
    """
    if key == "due":
        return parse_due(value)
    if key == "status" and value not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {value!r}")
    if key == "kind" and value not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {value!r}")
    return value


@runtime_checkable
class Store(Protocol):
    """The operations every storage backend must support."""

    def add(
        self,
        raw: str,
        *,
        title: Optional[str] = None,
        kind: Kind = "task",
        domain: str = "other",
        due: Optional[str] = None,
        agent: Optional[str] = None,
        dump_id: Optional[str] = None,
    ) -> Item: ...

    def all(self) -> list[Item]: ...

    def list(
        self,
        *,
        kind: Optional[Kind] = None,
        status: Optional[Status] = None,
        domain: Optional[str] = None,
    ) -> list[Item]: ...

    def get(self, item_id: str) -> Optional[Item]: ...

    def update(self, item_id: str, **changes) -> Optional[Item]: ...

    def set_status(self, item_id: str, status: Status) -> Optional[Item]: ...

    def complete(self, item_id: str) -> Optional[Item]: ...

    def remove(self, item_id: str) -> bool: ...


class JSONStore:
    """A :class:`Store` backed by a single JSON document on disk."""

    #: Bump if the on-disk shape ever changes in a breaking way.
    SCHEMA_VERSION = 1

    #: Fields that must never be overwritten by :meth:`update`.
    _IMMUTABLE = frozenset({"id", "created_at"})

    #: Fields :meth:`update` may touch — see module-level UPDATABLE_FIELDS.
    _UPDATABLE = UPDATABLE_FIELDS

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else default_store_path()
        # Every mutation is a read-modify-write over the whole file. Under the
        # command desk's threaded HTTP server two concurrent writers each
        # rewrite the file from their own read and the last one wins — the
        # other's items silently vanish. All in-process writers share the one
        # injected store instance, so an instance lock closes that race.
        # (Cross-process safety — CLI while the server runs — is the SQLite
        # backend's job, not this file's.)
        self._lock = threading.RLock()

    # --- low-level read / write ---------------------------------------------
    def _read(self) -> list[Item]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                doc = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise StoreError(f"Could not read store at {self.path}: {exc}") from exc
        try:
            return [Item.from_dict(d) for d in doc.get("items", [])]
        except TypeError as exc:
            # A record missing a required field is corruption, not a crash:
            # surface it as the error every caller already handles.
            raise StoreError(f"Malformed item record in {self.path}: {exc}") from exc

    def _write(self, items: list[Item]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "schema_version": self.SCHEMA_VERSION,
            "items": [item.to_dict() for item in items],
        }
        # Atomic write: serialize to a temp file in the same directory, then
        # os.replace() over the target (an atomic rename on Windows and POSIX).
        fd, tmp_path = tempfile.mkstemp(
            dir=self.path.parent, prefix=".plate-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp_path, self.path)
        except BaseException:
            # Never leave a half-written temp file behind.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # --- operations ----------------------------------------------------------
    def add(
        self,
        raw: str,
        *,
        title: Optional[str] = None,
        kind: Kind = "task",
        domain: str = "other",
        due: Optional[str] = None,
        agent: Optional[str] = None,
        dump_id: Optional[str] = None,
    ) -> Item:
        item = Item.create(
            raw,
            title=title,
            kind=kind,
            domain=domain,
            due=due,
            agent=agent,
            dump_id=dump_id,
        )
        with self._lock:
            items = self._read()
            items.append(item)
            self._write(items)
        return item

    def all(self) -> list[Item]:
        return self._read()

    def list(
        self,
        *,
        kind: Optional[Kind] = None,
        status: Optional[Status] = None,
        domain: Optional[str] = None,
    ) -> list[Item]:
        return [
            item
            for item in self._read()
            if (kind is None or item.kind == kind)
            and (status is None or item.status == status)
            and (domain is None or item.domain == domain)
        ]

    def get(self, item_id: str) -> Optional[Item]:
        return next((item for item in self._read() if item.id == item_id), None)

    def update(self, item_id: str, **changes) -> Optional[Item]:
        """Apply field changes to an item and persist. Returns it, or None.

        Unknown and immutable fields (id, created_at) are ignored so a bad key
        can't corrupt the record. Values that every read view depends on are
        validated here — one malformed due date would otherwise brick the
        agenda, the briefing, and the desk's /api/state at once.
        """
        with self._lock:
            items = self._read()
            for item in items:
                if item.id == item_id:
                    for key, value in changes.items():
                        if key not in self._UPDATABLE:
                            continue
                        setattr(item, key, _validated_change(key, value))
                    item.touch()
                    self._write(items)
                    return item
        return None

    def set_status(self, item_id: str, status: Status) -> Optional[Item]:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
        return self.update(item_id, status=status)

    def complete(self, item_id: str) -> Optional[Item]:
        return self.set_status(item_id, "done")

    def remove(self, item_id: str) -> bool:
        """Delete an item by id. Returns True if something was removed."""
        with self._lock:
            items = self._read()
            kept = [item for item in items if item.id != item_id]
            if len(kept) == len(items):
                return False
            self._write(kept)
        return True


# --- SQLite backend ----------------------------------------------------------

#: Store paths with these suffixes get the SQLite backend from create_store().
SQLITE_SUFFIXES: frozenset[str] = frozenset({".db", ".sqlite", ".sqlite3"})


class SQLiteStore:
    """A :class:`Store` backed by SQLite.

    Same semantics as :class:`JSONStore`, different guarantees: WAL mode plus
    per-operation connections give real **cross-process** safety — the CLI, the
    desk, and a scheduled `jarvis notify` job can all write at once without the
    lost-update hazard the JSON file's in-process lock can't cover. Opt in by
    pointing ``JARVIS_STORE_PATH`` at a ``.db`` file (see :func:`create_store`);
    move existing items over with ``jarvis migrate-store``.
    """

    _COLUMNS = (
        "id", "created_at", "updated_at", "raw", "title",
        "kind", "domain", "status", "due", "agent", "dump_id",
    )

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else default_store_path()
        self._init_schema()

    # --- connection ----------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None puts the driver in autocommit so transactions are
        # explicit. The default ('') defers BEGIN until the first DML, which
        # would leave a read-modify-write's SELECT outside the transaction —
        # exactly the window where a concurrent writer's update gets clobbered.
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def _session(self, *, write: bool = False):
        """A connection that is always closed, optionally inside a write txn.

        ``write=True`` opens ``BEGIN IMMEDIATE``, taking SQLite's write lock up
        front so a read-modify-write is serialized against every other writer —
        thread or process — for the whole span, not just the UPDATE.
        """
        conn = self._connect()
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.execute("COMMIT")
        except BaseException:
            if write:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._session() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS items (
                        id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        raw TEXT NOT NULL,
                        title TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        status TEXT NOT NULL,
                        due TEXT,
                        agent TEXT,
                        dump_id TEXT
                    )"""
                )
        except (sqlite3.Error, OSError) as exc:
            raise StoreError(f"Could not open store at {self.path}: {exc}") from exc

    @staticmethod
    def _to_item(row: sqlite3.Row) -> Item:
        return Item.from_dict(dict(row))

    def _insert(self, conn: sqlite3.Connection, item: Item) -> None:
        placeholders = ", ".join("?" for _ in self._COLUMNS)
        values = tuple(getattr(item, column) for column in self._COLUMNS)
        conn.execute(
            f"INSERT INTO items ({', '.join(self._COLUMNS)}) VALUES ({placeholders})",
            values,
        )

    # --- operations ----------------------------------------------------------
    def add(
        self,
        raw: str,
        *,
        title: Optional[str] = None,
        kind: Kind = "task",
        domain: str = "other",
        due: Optional[str] = None,
        agent: Optional[str] = None,
        dump_id: Optional[str] = None,
    ) -> Item:
        item = Item.create(
            raw, title=title, kind=kind, domain=domain,
            due=due, agent=agent, dump_id=dump_id,
        )
        try:
            with self._session(write=True) as conn:
                self._insert(conn, item)
        except sqlite3.Error as exc:
            raise StoreError(f"Could not write store at {self.path}: {exc}") from exc
        return item

    def all(self) -> list[Item]:
        try:
            with self._session() as conn:
                rows = conn.execute("SELECT * FROM items ORDER BY rowid").fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"Could not read store at {self.path}: {exc}") from exc
        return [self._to_item(row) for row in rows]

    def list(
        self,
        *,
        kind: Optional[Kind] = None,
        status: Optional[Status] = None,
        domain: Optional[str] = None,
    ) -> list[Item]:
        clauses, values = [], []
        for column, value in (("kind", kind), ("status", status), ("domain", domain)):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            with self._session() as conn:
                rows = conn.execute(
                    f"SELECT * FROM items{where} ORDER BY rowid", values
                ).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"Could not read store at {self.path}: {exc}") from exc
        return [self._to_item(row) for row in rows]

    def get(self, item_id: str) -> Optional[Item]:
        try:
            with self._session() as conn:
                row = conn.execute(
                    "SELECT * FROM items WHERE id = ?", (item_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError(f"Could not read store at {self.path}: {exc}") from exc
        return self._to_item(row) if row else None

    def update(self, item_id: str, **changes) -> Optional[Item]:
        """Same contract as JSONStore.update — validated, immutables ignored.

        The read-validate-write runs under ``BEGIN IMMEDIATE``, so the write
        lock is held across the SELECT as well as the UPDATE and concurrent
        writers — threads or other processes — serialize instead of silently
        overwriting each other from stale reads.
        """
        try:
            with self._session(write=True) as conn:
                row = conn.execute(
                    "SELECT * FROM items WHERE id = ?", (item_id,)
                ).fetchone()
                if row is None:
                    return None
                item = self._to_item(row)
                for key, value in changes.items():
                    if key not in UPDATABLE_FIELDS:
                        continue
                    setattr(item, key, _validated_change(key, value))
                item.touch()
                assignments = ", ".join(f"{c} = ?" for c in self._COLUMNS if c != "id")
                values = tuple(
                    getattr(item, c) for c in self._COLUMNS if c != "id"
                ) + (item_id,)
                conn.execute(f"UPDATE items SET {assignments} WHERE id = ?", values)
        except sqlite3.Error as exc:
            raise StoreError(f"Could not write store at {self.path}: {exc}") from exc
        return item

    def set_status(self, item_id: str, status: Status) -> Optional[Item]:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
        return self.update(item_id, status=status)

    def complete(self, item_id: str) -> Optional[Item]:
        return self.set_status(item_id, "done")

    def remove(self, item_id: str) -> bool:
        try:
            with self._session(write=True) as conn:
                cursor = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise StoreError(f"Could not write store at {self.path}: {exc}") from exc


# --- choosing and migrating backends -----------------------------------------

def create_store(path: Path | str | None = None) -> Store:
    """The store for a given path: SQLite for .db/.sqlite files, else JSON.

    This is the seam everything else calls, so switching backends is a path
    change (``JARVIS_STORE_PATH=.../plate.db``), not a code change.
    """
    resolved = Path(path) if path is not None else default_store_path()
    if resolved.suffix.lower() in SQLITE_SUFFIXES:
        return SQLiteStore(resolved)
    return JSONStore(resolved)


def migrate_items(source: Store, target: Store) -> int:
    """Copy every item from one store to another, preserving ids and timestamps.

    Refuses a non-empty target — merging two histories silently would be a
    great way to corrupt both. Returns the number of items copied.
    """
    if target.all():
        raise StoreError("target store already has items; refusing to merge")
    items = source.all()
    if isinstance(target, SQLiteStore):
        try:
            with target._session(write=True) as conn:
                for item in items:
                    target._insert(conn, item)
        except sqlite3.Error as exc:
            raise StoreError(f"Could not write store at {target.path}: {exc}") from exc
    elif isinstance(target, JSONStore):
        target._write(items)
    else:  # pragma: no cover - future backends supply their own bulk path
        raise StoreError(f"don't know how to migrate into {type(target).__name__}")
    return len(items)
