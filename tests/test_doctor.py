"""Tests for jarvis doctor. Report-only, offline, injected paths."""

import json
import tempfile
from pathlib import Path

from jarvis.agents import RESERVED_NAMES
from jarvis.doctor import run_doctor
from jarvis.dumps import DumpLog
from jarvis.ledger import CostLedger
from jarvis.registry import AgentRegistry
from jarvis.storage import JSONStore


def _paths():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-doctor-"))
    return {
        "store_path": tmp / "plate.json",
        "dumps_path": tmp / "dumps.jsonl",
        "ledger_path": tmp / "costs.jsonl",
        "registry_path": tmp / "agents.json",
    }


def test_missing_files_are_healthy_not_broken():
    report = run_doctor(**_paths(), reserved=RESERVED_NAMES)
    assert report.ok()
    assert "All clear" in report.render()


def test_healthy_populated_files_pass():
    paths = _paths()
    store = JSONStore(paths["store_path"])
    store.add("fine item", due="2026-09-01")
    DumpLog(paths["dumps_path"]).append("a dump")
    CostLedger(paths["ledger_path"]).append(0.10)
    AgentRegistry(paths["registry_path"], reserved=RESERVED_NAMES).add(
        "debate", "Debate prep.", "You are the debate specialist.", domain="debate"
    )
    assert run_doctor(**paths, reserved=RESERVED_NAMES).ok()


def test_bad_item_values_are_reported_with_ids():
    paths = _paths()
    store = JSONStore(paths["store_path"])
    item = store.add("fine")
    doc = json.loads(paths["store_path"].read_text())
    doc["items"][0]["due"] = "garbage"
    doc["items"][0]["kind"] = "wish"
    doc["items"].append(doc["items"][0].copy())          # duplicate id
    doc["items"].append({"title": "no id or timestamps"})  # missing fields
    paths["store_path"].write_text(json.dumps(doc))

    report = run_doctor(**paths, reserved=RESERVED_NAMES)
    rendered = report.render()
    assert not report.ok()
    assert f"duplicate id {item.id!r}" in rendered
    assert "malformed due date" in rendered
    assert "unknown kind 'wish'" in rendered
    assert "missing required field" in rendered


def test_corrupt_log_lines_and_unreadable_plate_are_reported():
    paths = _paths()
    paths["store_path"].parent.mkdir(parents=True, exist_ok=True)
    paths["store_path"].write_text("{truncated", encoding="utf-8")
    paths["dumps_path"].write_text('{"id":"ok1","text":"x"}\n{broken\n', encoding="utf-8")
    paths["ledger_path"].write_text('not json at all\n', encoding="utf-8")

    report = run_doctor(**paths, reserved=RESERVED_NAMES)
    rendered = report.render()
    assert "plate.json: unreadable" in rendered
    assert "dumps.jsonl: line 2" in rendered
    assert "costs.jsonl: line 1" in rendered


def test_invalid_registry_records_are_reported():
    paths = _paths()
    paths["registry_path"].parent.mkdir(parents=True, exist_ok=True)
    paths["registry_path"].write_text(json.dumps({
        "schema_version": 1,
        "agents": [
            {"name": "generalist", "description": "d", "prompt": "p"},   # reserved
            {"name": "ok-agent", "description": "", "prompt": "p"},      # no description
            {"name": "tooly", "description": "d", "prompt": "p", "tools": ["Bash"]},
        ],
    }), encoding="utf-8")

    report = run_doctor(**paths, reserved=RESERVED_NAMES)
    rendered = report.render()
    assert not report.ok()
    assert "generalist" in rendered            # can't shadow a built-in
    assert "description and a prompt" in rendered
    assert "outside the allowlist" in rendered


def test_doctor_never_modifies_anything():
    paths = _paths()
    paths["store_path"].parent.mkdir(parents=True, exist_ok=True)
    paths["store_path"].write_text("{broken", encoding="utf-8")
    before = paths["store_path"].read_text()
    run_doctor(**paths, reserved=RESERVED_NAMES)
    assert paths["store_path"].read_text() == before


# --- adversarial input must be reported, never crash -------------------------

def test_doctor_survives_hostile_shapes():
    """Every shape below either crashes a naive checker or is silently
    mis-read by the real readers. The doctor must report, not raise."""
    paths = _paths()
    for name in paths:
        paths[name].parent.mkdir(parents=True, exist_ok=True)

    paths["store_path"].write_text(json.dumps([1, 2, 3]))            # not an object
    paths["registry_path"].write_text(json.dumps({"agents": "nope"}))  # not a list
    paths["ledger_path"].write_text(json.dumps({"id": "x", "cost_usd": "lots"}) + "\n")
    paths["dumps_path"].write_text("[]\n")                          # not an object

    report = run_doctor(**paths, reserved=RESERVED_NAMES)   # must not raise
    rendered = report.render()
    assert "top level is not an object" in rendered
    assert "'agents' is not a list" in rendered
    assert "cost is not a number" in rendered


def test_doctor_reports_unhashable_ids_and_bad_agent_types():
    paths = _paths()
    for name in paths:
        paths[name].parent.mkdir(parents=True, exist_ok=True)
    paths["store_path"].write_text(json.dumps({"items": [
        {"id": ["a"], "created_at": 1, "updated_at": 1, "raw": "r", "title": "t"},
    ]}))
    paths["registry_path"].write_text(json.dumps({"agents": [
        {"name": 123, "description": "d", "prompt": "p"},   # non-string name
        "not-an-object",
    ]}))
    report = run_doctor(**paths, reserved=RESERVED_NAMES)   # must not raise
    rendered = report.render()
    assert "id is not a usable value" in rendered
    assert "agent #1 is not an object" in rendered
    assert not report.ok()


def test_a_plate_without_an_items_key_is_not_flagged():
    """JSONStore._read reads such a file fine, so the doctor must not cry wolf."""
    paths = _paths()
    paths["store_path"].parent.mkdir(parents=True, exist_ok=True)
    paths["store_path"].write_text(json.dumps({"schema_version": 1}))
    assert run_doctor(**paths, reserved=RESERVED_NAMES).ok()
