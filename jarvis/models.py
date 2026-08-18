"""Domain models for Jarvis.

The single unit of memory is an :class:`Item`: one thing the user dumped —
a project, a task, or a reminder. Models are plain, serializable dataclasses
with no storage or SDK dependencies, so they are trivial to test and reuse.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timezone
from typing import Literal, Optional

# --- Controlled vocabularies -------------------------------------------------
# Kept as ``Literal`` types (for static checking) plus tuples (for runtime
# validation). They serialize as plain strings, keeping the JSON human-readable.

Kind = Literal["project", "task", "reminder"]
Status = Literal["inbox", "active", "done"]

KINDS: tuple[Kind, ...] = ("project", "task", "reminder")
STATUSES: tuple[Status, ...] = ("inbox", "active", "done")

#: Domains are free-form (not enforced), but these are the ones Jarvis knows the
#: user cares about. Surfaced to the model so it labels items consistently.
SUGGESTED_DOMAINS: tuple[str, ...] = (
    "club",
    "rebrand",
    "startup",
    "homework",
    "leetcode",
    "internships",
    "other",
)

DUE_FORMAT = "%Y-%m-%d"


def _utcnow_iso() -> str:
    """Current UTC time as a stable, sortable ISO-8601 string (second precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    """A short, collision-resistant id that's easy for a human to type back."""
    return uuid.uuid4().hex[:8]


def parse_due(value: Optional[str]) -> Optional[str]:
    """Normalize/validate a due date. Returns a ``YYYY-MM-DD`` string or None.

    Empty/None becomes None. A non-empty value must be a valid ``YYYY-MM-DD``
    date, otherwise :class:`ValueError` is raised — so a malformed date can never
    reach the store and silently break the agenda view.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        datetime.strptime(text, DUE_FORMAT)
    except ValueError as exc:
        raise ValueError(f"due must be a YYYY-MM-DD date, got {value!r}") from exc
    return text


@dataclass
class Item:
    """One captured thought.

    ``raw`` preserves exactly what the user said; ``title`` is a cleaned
    one-liner Jarvis can show in lists. ``id`` is short and stable so the user
    (or an agent) can reference an item to complete or update it.
    """

    id: str
    created_at: str
    updated_at: str
    raw: str
    title: str
    kind: Kind = "task"
    domain: str = "other"
    status: Status = "inbox"
    due: Optional[str] = None      # ISO date (YYYY-MM-DD), or None
    agent: Optional[str] = None    # specialist agent that owns this, if any
    dump_id: Optional[str] = None  # the brain-dump this came from, if any

    @classmethod
    def create(
        cls,
        raw: str,
        *,
        title: Optional[str] = None,
        kind: Kind = "task",
        domain: str = "other",
        due: Optional[str] = None,
        agent: Optional[str] = None,
        dump_id: Optional[str] = None,
    ) -> "Item":
        """Build a new item with a fresh id and timestamps. Validates kind and due."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
        now = _utcnow_iso()
        return cls(
            id=_new_id(),
            created_at=now,
            updated_at=now,
            raw=raw.strip(),
            title=(title or raw).strip(),
            kind=kind,
            domain=(domain or "other").strip() or "other",
            status="inbox",
            due=parse_due(due),
            agent=agent,
            dump_id=dump_id,
        )

    def touch(self) -> None:
        """Refresh ``updated_at`` to now."""
        self.updated_at = _utcnow_iso()

    def due_date(self) -> Optional[date]:
        """The due date as a ``date`` object, or None if unset or unparseable.

        Tolerating a malformed stored date (hand-edited JSON, an old bug) means
        one bad item degrades to "undated" instead of crashing every read view.
        """
        if not self.due:
            return None
        try:
            return datetime.strptime(self.due, DUE_FORMAT).date()
        except (ValueError, TypeError):
            # TypeError guards a non-string stored value (e.g. an unquoted JSON
            # number from a hand-edit): degrade to "undated", never crash the
            # agenda, briefing, and /api/state that read every item.
            return None

    def updated_dt(self) -> Optional[datetime]:
        """``updated_at`` as a timezone-aware datetime, or None if unparseable."""
        try:
            parsed = datetime.fromisoformat(self.updated_at)
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def age_days(self, now: Optional[datetime] = None) -> Optional[int]:
        """Whole days since this item was last touched, or None if unknown."""
        updated = self.updated_dt()
        if updated is None:
            return None
        return max(0, ((now or datetime.now(timezone.utc)) - updated).days)

    # --- serialization -------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Item":
        """Rebuild an item from stored JSON, tolerating unknown/missing keys.

        Being lenient here means adding a field to the model later won't break
        reading an older store file.
        """
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        due = clean.get("due")
        if due is not None and not isinstance(due, str):
            # A non-string due can only be corruption; normalize it away rather
            # than carry a value every date operation would choke on.
            clean["due"] = None
        return cls(**clean)


def format_item(item: Item) -> str:
    """One-line, id-first rendering shared by the tools and the CLI.

    Id first so it can be quoted back to complete/remove the item. Kept to plain
    ASCII so it renders correctly in the Windows console (cp1252 can't encode
    typographic characters like U+00B7).
    """
    due = f" | due {item.due}" if item.due else ""
    agent = f" | @{item.agent}" if item.agent else ""
    return f"[{item.id}] ({item.status}) {item.title} - {item.kind}/{item.domain}{due}{agent}"
