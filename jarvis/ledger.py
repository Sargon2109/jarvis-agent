"""The cost ledger — what Jarvis actually spends.

Every orchestrator run costs real money, and a student budget deserves the same
courtesy the plate gets: nothing forgotten. Each run appends one line here, so
"what has Jarvis cost me this month?" is a local file read, not a trip to the
billing console.

Same shape as :mod:`jarvis.dumps` on purpose: append-only JSONL, torn writes
can only damage the last line, corrupt lines are skipped on read.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

#: Fallback location: <repo>/data/costs.jsonl (the data/ dir is gitignored).
DEFAULT_LEDGER_PATH = Path(__file__).resolve().parent.parent / "data" / "costs.jsonl"

#: Env var to override where the ledger lives.
LEDGER_PATH_ENV = "JARVIS_LEDGER_PATH"


def default_ledger_path() -> Path:
    """Where the ledger lives: ``$JARVIS_LEDGER_PATH`` if set, else the default."""
    override = os.environ.get(LEDGER_PATH_ENV)
    return Path(override) if override else DEFAULT_LEDGER_PATH


@dataclass
class CostEntry:
    """One orchestrator run's bill."""

    id: str
    created_at: str            # ISO-8601 UTC
    cost_usd: float
    turns: int = 0
    duration_ms: int = 0
    dump_id: Optional[str] = None  # the brain-dump that triggered the spend

    @classmethod
    def create(
        cls,
        cost_usd: float,
        *,
        turns: int = 0,
        duration_ms: int = 0,
        dump_id: Optional[str] = None,
    ) -> "CostEntry":
        return cls(
            id=uuid.uuid4().hex[:8],
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            cost_usd=float(cost_usd),
            turns=turns,
            duration_ms=duration_ms,
            dump_id=dump_id,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "CostEntry":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class CostLedger:
    """An append-only JSONL ledger of :class:`CostEntry` records.

    Reads are deliberately forgiving — the ledger is bookkeeping, and a bad
    line must never take down the briefing or the desk that asked for a total.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else default_ledger_path()
        self._lock = threading.Lock()

    def append(
        self,
        cost_usd: float,
        *,
        turns: int = 0,
        duration_ms: int = 0,
        dump_id: Optional[str] = None,
    ) -> CostEntry:
        """Record one run's cost. OSError propagates; callers decide how loud to be."""
        entry = CostEntry.create(
            cost_usd, turns=turns, duration_ms=duration_ms, dump_id=dump_id
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        return entry

    def all(self) -> list[CostEntry]:
        """Every entry, oldest first. Corrupt lines are skipped, not fatal."""
        if not self.path.exists():
            return []
        entries: list[CostEntry] = []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(CostEntry.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
        except OSError:
            return []
        return entries

    def total(self) -> float:
        """Everything ever spent, in USD."""
        return sum(e.cost_usd for e in self.all())

    def month_total(self, now: Optional[datetime] = None) -> float:
        """Spend in the current UTC calendar month."""
        now = now or datetime.now(timezone.utc)
        prefix = f"{now.year:04d}-{now.month:02d}"
        return sum(e.cost_usd for e in self.all() if e.created_at.startswith(prefix))
