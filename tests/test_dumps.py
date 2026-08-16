"""Tests for the append-only dump log. No API calls."""

import tempfile
from pathlib import Path

from jarvis.dumps import DumpLog, DumpRecord


def _log() -> DumpLog:
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-dumps-"))
    return DumpLog(tmpdir / "dumps.jsonl")


def test_append_returns_record_and_persists():
    log = _log()
    record = log.append("start a club, grind leetcode")
    assert record.id and record.created_at
    reopened = DumpLog(log.path)
    assert len(reopened.all()) == 1
    assert reopened.all()[0].text == "start a club, grind leetcode"


def test_missing_file_is_empty_not_an_error():
    assert _log().all() == []


def test_appends_accumulate_in_order():
    log = _log()
    for text in ("first", "second", "third"):
        log.append(text)
    assert [r.text for r in log.all()] == ["first", "second", "third"]


def test_recent_returns_newest_first_and_respects_limit():
    log = _log()
    for text in ("first", "second", "third"):
        log.append(text)
    assert [r.text for r in log.recent()] == ["third", "second", "first"]
    assert [r.text for r in log.recent(limit=2)] == ["third", "second"]


def test_get_returns_match_or_none():
    log = _log()
    record = log.append("something")
    assert log.get(record.id).text == "something"
    assert log.get("deadbeef") is None


def test_corrupt_line_is_skipped_not_fatal():
    """A torn write must never cost you the rest of your history."""
    log = _log()
    log.append("good one")
    with log.path.open("a", encoding="utf-8") as f:
        f.write("{ this line is broken\n")
    log.append("good two")
    texts = [r.text for r in log.all()]
    assert texts == ["good one", "good two"]


def test_blank_lines_are_ignored():
    log = _log()
    log.append("kept")
    with log.path.open("a", encoding="utf-8") as f:
        f.write("\n\n")
    assert len(log.all()) == 1


def test_unicode_survives_a_round_trip():
    log = _log()
    log.append("rebrand — the logo's dated ✦")
    assert log.all()[0].text == "rebrand — the logo's dated ✦"


def test_source_is_recorded():
    log = _log()
    assert log.append("x", source="cli").source == "cli"
    assert log.append("y").source == "llm"


def test_summary_truncates_long_text_and_flattens_newlines():
    record = DumpRecord.create("word " * 100)
    assert len(record.summary(width=40)) == 40
    multiline = DumpRecord.create("first line\n\nsecond line")
    assert "\n" not in multiline.summary()


def test_text_is_stripped():
    assert DumpRecord.create("   padded   ").text == "padded"
