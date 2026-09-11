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


# --- one run, one cost -------------------------------------------------------

def test_repeated_snapshots_of_one_run_are_counted_once():
    """Entries written before the desk knew total_cost_usd was a running total
    hold several rows per run, each a larger snapshot of the same spend."""
    ledger = _ledger()
    ledger.append(0.1887, dump_id="run-1")
    ledger.append(1.2750, dump_id="run-1")
    ledger.append(3.0336, dump_id="run-1")
    assert abs(ledger.total() - 3.0336) < 1e-9


def test_separate_runs_still_add_up():
    ledger = _ledger()
    ledger.append(1.00, dump_id="run-1")
    ledger.append(2.00, dump_id="run-2")
    assert abs(ledger.total() - 3.00) < 1e-9


def test_entries_without_a_run_id_are_each_counted():
    """Nothing groups them, so they must not silently collapse into one."""
    ledger = _ledger()
    ledger.append(0.50)
    ledger.append(0.50)
    assert abs(ledger.total() - 1.00) < 1e-9


def test_the_month_total_deduplicates_the_same_way():
    ledger = _ledger()
    ledger.append(0.10, dump_id="run-1")
    ledger.append(0.90, dump_id="run-1")
    assert abs(ledger.month_total() - 0.90) < 1e-9
