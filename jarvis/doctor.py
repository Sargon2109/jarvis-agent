"""``jarvis doctor`` — check the data files and report, never repair.

The data layout invites hand-editing (plain JSON, gitignored, documented), and
a hand-edit that goes wrong should be *diagnosed*, not silently tolerated or
crashed on. This module reads every data file the way the real readers do and
reports what would trip them: unparseable documents, malformed records, invalid
values, duplicate ids, corrupt log lines.

Report-only is a deliberate boundary: automatically "fixing" the user's memory
risks destroying the very thing Jarvis promises to keep. The doctor says what's
wrong and where; the user decides what to do about it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .dumps import default_dumps_path
from .ledger import default_ledger_path
from .models import KINDS, STATUSES, parse_due
from .registry import AgentRecord, default_registry_path, validate_record, RegistryError
from .storage import default_store_path

#: Item fields that must be present for the store to rebuild a record.
_REQUIRED_ITEM_FIELDS = ("id", "created_at", "updated_at", "raw", "title")


@dataclass
class Report:
    """Everything the doctor found, per file."""

    issues: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    def problem(self, path: Path, message: str) -> None:
        self.issues.append(f"{path.name}: {message}")

    def ok(self) -> bool:
        return not self.issues

    def render(self) -> str:
        lines = [f"Checked: {', '.join(self.checked)}"]
        if self.ok():
            lines.append("All clear — every data file is readable and valid.")
        else:
            lines.append(f"{len(self.issues)} issue(s) found:")
            lines += [f"  - {issue}" for issue in self.issues]
            lines.append(
                "The doctor never edits your data. Fix by hand, restore a "
                "backup, or delete the offending record."
            )
        return "\n".join(lines)


def _load_json(path: Path, report: Report):
    """Parse a JSON file the defensive way. Returns the document or None.

    A doctor that crashes on the file it was asked to diagnose is useless, so
    every failure mode — bad syntax, unreadable file, or nesting deep enough to
    blow the interpreter's recursion limit — becomes a reported issue.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        report.problem(path, f"unreadable: {exc}")
    except RecursionError:
        report.problem(path, "JSON nested too deeply to parse")
    return None


def check_plate(path: Path, report: Report) -> None:
    report.checked.append(path.name)
    if not path.exists():
        return  # a store that doesn't exist yet is healthy, not broken
    doc = _load_json(path, report)
    if doc is None:
        return
    if not isinstance(doc, dict):
        report.problem(path, "top level is not an object — is this a plate file?")
        return
    items = doc.get("items", [])
    if not isinstance(items, list):
        report.problem(path, "'items' is not a list — is this really a plate file?")
        return

    seen_ids: set = set()
    for index, record in enumerate(items):
        where = f"item #{index}"
        if not isinstance(record, dict):
            report.problem(path, f"{where} is not an object")
            continue
        item_id = record.get("id")
        if item_id:
            try:
                if item_id in seen_ids:
                    report.problem(path, f"duplicate id {item_id!r}")
                seen_ids.add(item_id)
                where = f"item {item_id}"
            except TypeError:
                # An unhashable id (list/dict) — the real reader would store it
                # happily and every lookup by id would then silently miss.
                report.problem(path, f"{where}: id is not a usable value ({item_id!r})")
        for required in _REQUIRED_ITEM_FIELDS:
            if required not in record:
                report.problem(path, f"{where}: missing required field {required!r}")
        kind = record.get("kind")
        if kind is not None and kind not in KINDS:
            report.problem(path, f"{where}: unknown kind {kind!r}")
        status = record.get("status")
        if status is not None and status not in STATUSES:
            report.problem(path, f"{where}: unknown status {status!r}")
        due = record.get("due")
        if due is not None:
            if not isinstance(due, str):
                report.problem(path, f"{where}: due is not a string ({due!r})")
            else:
                try:
                    parse_due(due)
                except (ValueError, TypeError):
                    report.problem(path, f"{where}: malformed due date {due!r}")


def _check_jsonl(path: Path, report: Report, label: str) -> None:
    report.checked.append(path.name)
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        report.problem(path, f"unreadable: {exc}")
        return
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            report.problem(path, f"line {number}: corrupt {label} record (bad JSON)")
            continue
        if not isinstance(record, dict) or "id" not in record:
            report.problem(path, f"line {number}: {label} record has no id")
            continue
        # A ledger entry whose cost isn't a number crashes CostLedger.total().
        if label == "ledger":
            cost = record.get("cost_usd")
            if not isinstance(cost, (int, float)) or isinstance(cost, bool):
                report.problem(path, f"line {number}: ledger cost is not a number ({cost!r})")


def check_registry(path: Path, report: Report, *, reserved: frozenset[str]) -> None:
    report.checked.append(path.name)
    if not path.exists():
        return
    doc = _load_json(path, report)
    if doc is None:
        return
    if not isinstance(doc, dict):
        report.problem(path, "top level is not an object — is this a registry file?")
        return
    agents = doc.get("agents", [])
    if not isinstance(agents, list):
        report.problem(path, "'agents' is not a list")
        return
    for index, record in enumerate(agents):
        if not isinstance(record, dict):
            report.problem(path, f"agent #{index} is not an object")
            continue
        name = record.get("name")
        where = f"agent {name!r}" if isinstance(name, str) and name else f"agent #{index}"
        try:
            validate_record(AgentRecord.from_dict(record), reserved=reserved)
        except (RegistryError, TypeError, AttributeError) as exc:
            # AttributeError: a non-string name/description reaches .strip().
            report.problem(path, f"{where}: {exc}")


def run_doctor(
    *,
    store_path: Optional[Path] = None,
    dumps_path: Optional[Path] = None,
    ledger_path: Optional[Path] = None,
    registry_path: Optional[Path] = None,
    reserved: frozenset[str] = frozenset(),
) -> Report:
    """Check every data file. Paths are injectable for tests."""
    report = Report()
    store = store_path or default_store_path()
    if store.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        # SQLite validates its own structure on open; the JSON checks below
        # would just misread the binary file.
        report.checked.append(store.name + " (sqlite — structural checks skipped)")
    else:
        check_plate(store, report)
    _check_jsonl(dumps_path or default_dumps_path(), report, "dump")
    _check_jsonl(ledger_path or default_ledger_path(), report, "ledger")
    check_registry(registry_path or default_registry_path(), report, reserved=reserved)
    return report
