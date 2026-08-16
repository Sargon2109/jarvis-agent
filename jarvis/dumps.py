"""The dump log — every brain-dump, kept verbatim.

The store holds the *items* Jarvis parsed out of what you said. This holds what
you actually said, word for word, so you can go back and re-read your own
thinking: "what was on my mind on August 3rd?"

It's a JSONL file — one JSON object per line, appended and never rewritten.
That's deliberate:
  * appending is O(1), so it stays fast no matter how long the log gets;
  * a torn write can only ever damage the last line, never the history;
  * a corrupt line is skipped on read rather than taking the whole log down.

Records are immutable. Whether a dump was ever processed is *derived* — an item
carries the ``dump_id`` it came from — so nothing here ever needs updating.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

#: Fallback location: <repo>/data/dumps.jsonl (the data/ dir is gitignored).
DEFAULT_DUMPS_PATH = Path(__file__).resolve().parent.parent / "data" / "dumps.jsonl"

#: Env var to override where the log lives.
DUMPS_PATH_ENV = "JARVIS_DUMPS_PATH"


def default_dumps_path() -> Path:
    """Where the log lives: ``$JARVIS_DUMPS_PATH`` if set, else the default."""
    override = os.environ.get(DUMPS_PATH_ENV)
    return Path(override) if override else DEFAULT_DUMPS_PATH


class DumpLogError(RuntimeError):
    """Raised when the log can't be read or written."""


@dataclass
class DumpRecord:
    """One brain-dump, exactly as the user gave it."""

    id: str
    created_at: str
    text: str
    source: str = "llm"  # "llm" (routed by the orchestrator) or "cli" (captured raw)

    @classmethod
    def create(cls, text: str, *, source: str = "llm") -> "DumpRecord":
        return cls(
            id=uuid.uuid4().hex[:8],
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            text=text.strip(),
            source=source,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "DumpRecord":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def summary(self, width: int = 72) -> str:
        """A one-line preview of the dump, for listings."""
        flat = " ".join(self.text.split())
        return flat if len(flat) <= width else flat[: width - 1] + "…"


class DumpLog:
    """An append-only JSONL log of :class:`DumpRecord`s."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else default_dumps_path()

    def append(self, text: str, *, source: str = "llm") -> DumpRecord:
        """Record a dump. Returns the stored record (its id links items back here)."""
        record = DumpRecord.create(text, source=source)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        except OSError as exc:
            raise DumpLogError(f"Could not write dump log at {self.path}: {exc}") from exc
        return record

    def all(self) -> list[DumpRecord]:
        """Every dump, oldest first. Corrupt lines are skipped, not fatal."""
        if not self.path.exists():
            return []
        records: list[DumpRecord] = []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(DumpRecord.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, TypeError):
                        continue  # one bad line must not lose the rest of the history
        except OSError as exc:
            raise DumpLogError(f"Could not read dump log at {self.path}: {exc}") from exc
        return records

    def recent(self, limit: int = 10) -> list[DumpRecord]:
        """The most recent dumps, newest first."""
        return list(reversed(self.all()))[:limit]

    def get(self, dump_id: str) -> Optional[DumpRecord]:
        return next((r for r in self.all() if r.id == dump_id), None)
