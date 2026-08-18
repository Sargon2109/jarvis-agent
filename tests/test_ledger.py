"""Tests for the cost ledger. No API calls."""

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from jarvis.ledger import CostLedger


def _ledger() -> CostLedger:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-ledger-"))
    return CostLedger(tmp / "costs.jsonl")


def test_append_and_totals():
    ledger = _ledger()
    ledger.append(0.25, turns=5, duration_ms=1000, dump_id="d1")
    ledger.append(0.50, turns=8, duration_ms=2000)
    assert abs(ledger.total() - 0.75) < 1e-9
    entries = ledger.all()
    assert entries[0].dump_id == "d1"
    assert entries[1].dump_id is None


def test_month_total_counts_only_this_month():
    ledger = _ledger()
    ledger.append(0.30)
    # Forge an entry from another month by writing the line directly.
    old = ledger.all()[0]
    with ledger.path.open("a", encoding="utf-8") as f:
        f.write(
            '{"id":"old00000","created_at":"2001-01-01T00:00:00+00:00","cost_usd":9.99}\n'
        )
    now = datetime.now(timezone.utc)
    assert abs(ledger.month_total(now) - 0.30) < 1e-9
    assert abs(ledger.total() - 10.29) < 1e-9
    assert old.cost_usd == 0.30


def test_corrupt_lines_are_skipped_not_fatal():
    ledger = _ledger()
    ledger.append(0.10)
    with ledger.path.open("a", encoding="utf-8") as f:
        f.write("{not json}\n")
    ledger.append(0.20)
    assert len(ledger.all()) == 2
    assert abs(ledger.total() - 0.30) < 1e-9


def test_missing_file_reads_as_empty():
    ledger = _ledger()
    assert ledger.all() == []
    assert ledger.total() == 0.0
    assert ledger.month_total() == 0.0
