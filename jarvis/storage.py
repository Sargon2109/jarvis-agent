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
import tempfile
import threading
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

    #: Fields :meth:`update` may touch — real Item fields only, so a stray key
    #: can neither invent attributes nor shadow a method like ``touch``.
    _UPDATABLE = frozenset(f.name for f in fields(Item)) - _IMMUTABLE

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
                        if key == "due":
                            value = parse_due(value)  # raises ValueError when malformed
                        elif key == "status" and value not in STATUSES:
                            raise ValueError(f"status must be one of {STATUSES}, got {value!r}")
                        elif key == "kind" and value not in KINDS:
                            raise ValueError(f"kind must be one of {KINDS}, got {value!r}")
                        setattr(item, key, value)
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
