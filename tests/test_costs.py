"""Tests for the cost breakdown. Offline, against injected temp ledgers."""

import tempfile
from pathlib import Path

from jarvis.costs import build_report, collapse
from jarvis.dumps import DumpLog
from jarvis.ledger import CostLedger


def _parts():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-costs-"))
    return CostLedger(tmp / "costs.jsonl"), DumpLog(tmp / "dumps.jsonl")


# --- collapsing duplicate rows ----------------------------------------------

def test_snapshots_of_one_run_collapse_to_its_final_cost():
    ledger, _ = _parts()
    ledger.append(0.19, dump_id="r1")
    ledger.append(1.27, dump_id="r1")
    ledger.append(3.03, dump_id="r1")
    runs = collapse(ledger.all())
    assert len(runs) == 1
    assert abs(runs[0].cost_usd - 3.03) < 1e-9


def test_separate_runs_are_kept_apart():
    ledger, _ = _parts()
    ledger.append(1.0, dump_id="r1")
    ledger.append(2.0, dump_id="r2")
    assert len(collapse(ledger.all())) == 2


def test_rows_without_a_run_id_are_not_merged():
    ledger, _ = _parts()
    ledger.append(0.5)
    ledger.append(0.5)
    assert len(collapse(ledger.all())) == 2


# --- the report --------------------------------------------------------------

def test_an_empty_ledger_says_so_rather_than_dividing_by_zero():
    ledger, log = _parts()
    assert "nothing has been spent" in build_report(ledger, log)


def test_the_report_names_the_expensive_run():
    ledger, log = _parts()
    dump = log.append("read every file in every course", source="llm")
    ledger.append(3.03, dump_id=dump.id, agents=4,
                  cache_read_tokens=400_000, output_tokens=2_000)
    cheap = log.append("what is due today", source="llm")
    ledger.append(0.01, dump_id=cheap.id)

    report = build_report(ledger, log)
    assert "read every file in every course" in report
    assert "$ 3.030" in report or "$3.030" in report
    assert "4 agents" in report


def test_the_report_contrasts_delegating_runs_with_plain_ones():
    """The delegation multiplier is the single most useful number here."""
    ledger, log = _parts()
    for _ in range(2):
        d = log.append("big", source="llm")
        ledger.append(0.70, dump_id=d.id, agents=3)
    for _ in range(2):
        d = log.append("small", source="llm")
        ledger.append(0.01, dump_id=d.id, agents=0)

    report = build_report(ledger, log)
    assert "Runs that delegated" in report
    assert "x more per run" in report


def test_token_counts_appear_when_recorded():
    ledger, log = _parts()
    d = log.append("something", source="llm")
    ledger.append(0.05, dump_id=d.id, cache_read_tokens=27_000, input_tokens=20)
    assert "27,020 tok" in build_report(ledger, log)


def test_runs_without_token_data_still_render():
    """Entries written before the token fields existed must not break it."""
    ledger, log = _parts()
    d = log.append("old run", source="llm")
    ledger.append(0.25, dump_id=d.id)
    assert "no token data" in build_report(ledger, log)


def test_top_limits_the_listing():
    ledger, log = _parts()
    for n in range(8):
        d = log.append(f"run {n}", source="llm")
        ledger.append(0.10 * (n + 1), dump_id=d.id)
    report = build_report(ledger, log, top=3)
    assert "run 7" in report and "run 0" not in report
