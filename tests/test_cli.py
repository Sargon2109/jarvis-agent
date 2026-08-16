"""Tests for the offline CLI. Drives cli.main() with an injected store. No API calls."""

import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from jarvis.agents import RESERVED_NAMES
from jarvis.cli import main as cli_main
from jarvis.dumps import DumpLog
from jarvis.registry import AgentRegistry
from jarvis.storage import JSONStore


def _store() -> JSONStore:
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-cli-"))
    return JSONStore(tmpdir / "plate.json")


def _run(argv, store) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli_main(argv, store=store)
    return code, buf.getvalue()


def test_add_then_list_shows_item():
    store = _store()
    code, out = _run(["add", "finish pset", "--domain", "homework"], store)
    assert code == 0 and "Captured" in out
    _, out = _run(["list"], store)
    assert "finish pset" in out and "homework" in out


def test_remind_then_agenda_shows_overdue():
    store = _store()
    _run(["remind", "book room", "--due", "2000-01-01"], store)  # long overdue
    code, out = _run(["agenda"], store)
    assert code == 0 and "OVERDUE" in out and "book room" in out


def test_done_hides_from_default_list_but_all_shows_it():
    store = _store()
    _run(["add", "task one"], store)
    item_id = store.all()[0].id
    code, out = _run(["done", item_id], store)
    assert code == 0 and "Done" in out
    _, out = _run(["list"], store)
    assert "task one" not in out            # hidden by default
    _, out = _run(["list", "--all"], store)
    assert "task one" in out                # shown with --all


def test_remove_deletes_item():
    store = _store()
    _run(["add", "temp"], store)
    item_id = store.all()[0].id
    code, out = _run(["remove", item_id], store)
    assert code == 0 and "Removed" in out
    assert store.all() == []


def test_done_unknown_id_returns_nonzero():
    store = _store()
    code, _ = _run(["done", "deadbeef"], store)
    assert code == 1


def test_invalid_due_reports_error():
    store = _store()
    code, out = _run(["remind", "x", "--due", "not-a-date"], store)
    assert code == 1 and "Error" in out
    assert store.all() == []


# --- dump log commands -------------------------------------------------------

def _log() -> DumpLog:
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-cli-log-"))
    return DumpLog(tmpdir / "dumps.jsonl")


def _run_log(argv, store, log) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli_main(argv, store=store, log=log)
    return code, buf.getvalue()


def test_dump_saves_verbatim_and_lists():
    store, log = _store(), _log()
    code, out = _run_log(["dump", "start a club and grind leetcode"], store, log)
    assert code == 0 and "Saved dump" in out
    assert log.all()[0].text == "start a club and grind leetcode"

    code, out = _run_log(["dumps"], store, log)
    assert code == 0 and "start a club" in out
    assert "*" in out  # not yet sorted into items


def test_dumps_empty_is_friendly():
    code, out = _run_log(["dumps"], _store(), _log())
    assert code == 0 and "No dumps recorded yet" in out


def test_dumps_show_displays_full_text_and_linked_items():
    store, log = _store(), _log()
    record = log.append("club stuff and leetcode")
    store.add("start a club", domain="club", dump_id=record.id)
    store.add("unrelated item", domain="other")

    code, out = _run_log(["dumps", "--show", record.id], store, log)
    assert code == 0
    assert "club stuff and leetcode" in out
    assert "Produced 1 item" in out
    assert "start a club" in out
    assert "unrelated item" not in out


def test_dumps_show_unknown_id_returns_nonzero():
    code, _ = _run_log(["dumps", "--show", "deadbeef"], _store(), _log())
    assert code == 1


def test_sorted_dump_is_not_starred():
    store, log = _store(), _log()
    record = log.append("something")
    store.add("an item", dump_id=record.id)
    code, out = _run_log(["dumps"], store, log)
    assert code == 0
    assert "not yet sorted" not in out


# --- agent commands ----------------------------------------------------------

def _registry() -> AgentRegistry:
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-cli-reg-"))
    return AgentRegistry(tmpdir / "agents.json", reserved=RESERVED_NAMES)


def _run_reg(argv, store, registry) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli_main(argv, store=store, registry=registry)
    return code, buf.getvalue()


def test_agents_lists_builtins_with_domains():
    code, out = _run_reg(["agents"], _store(), _registry())
    assert code == 0
    assert "generalist" in out
    assert "club [club] (built-in)" in out


def test_agents_show_prints_prompt():
    code, out = _run_reg(["agents", "--show", "club"], _store(), _registry())
    assert code == 0 and "club specialist" in out


def test_agents_show_unknown_returns_nonzero():
    code, _ = _run_reg(["agents", "--show", "nope"], _store(), _registry())
    assert code == 1


def test_custom_agent_listed_then_forgotten():
    store, registry = _store(), _registry()
    registry.add("debate", "Debate prep.", "You are the debate specialist.", domain="debate")

    code, out = _run_reg(["agents"], store, registry)
    assert code == 0 and "debate [debate] (custom)" in out

    code, out = _run_reg(["forget-agent", "debate"], store, registry)
    assert code == 0 and "Forgot" in out
    assert registry.records() == []


def test_forget_unknown_agent_returns_nonzero():
    code, _ = _run_reg(["forget-agent", "club"], _store(), _registry())
    assert code == 1  # built-ins can't be removed


# --- promote -----------------------------------------------------------------

def _seed(store, domain, n):
    for i in range(n):
        store.add(f"{domain} item {i}", domain=domain)


def test_promote_with_no_domain_lists_candidates():
    store, registry = _store(), _registry()
    _seed(store, "debate", 4)
    code, out = _run_reg(["promote"], store, registry)
    assert code == 0 and "debate" in out


def test_promote_creates_the_specialist():
    store, registry = _store(), _registry()
    _seed(store, "debate", 3)
    code, out = _run_reg(["promote", "debate"], store, registry)
    assert code == 0 and "Created the 'debate' specialist" in out
    assert registry.get("debate") is not None


def test_promote_below_threshold_is_refused_but_forceable():
    store, registry = _store(), _registry()
    _seed(store, "debate", 1)
    code, out = _run_reg(["promote", "debate"], store, registry)
    assert code == 1 and "under the threshold" in out
    assert registry.records() == []

    code, _ = _run_reg(["promote", "debate", "--force"], store, registry)
    assert code == 0 and registry.get("debate") is not None


def test_promote_refuses_a_domain_that_already_has_an_owner():
    store, registry = _store(), _registry()
    _seed(store, "club", 5)
    code, out = _run_reg(["promote", "club"], store, registry)
    assert code == 1 and "already has a specialist" in out


# --- brief -------------------------------------------------------------------

def _run_full(argv, store, registry, log) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli_main(argv, store=store, registry=registry, log=log)
    return code, buf.getvalue()


def test_brief_on_an_empty_system_is_quiet():
    code, out = _run_full(["brief"], _store(), _registry(), _log())
    assert code == 0 and "clear" in out.lower()


def test_brief_gathers_signals_across_all_three_files():
    store, registry, log = _store(), _registry(), _log()
    store.add("book the room", kind="reminder", due="2000-01-01")   # overdue
    log.append("never sorted this one")                             # unsorted dump
    _seed(store, "debate", 3)                                       # promotion candidate

    code, out = _run_full(["brief"], store, registry, log)
    assert code == 0
    assert "OVERDUE" in out
    assert "CAPTURED BUT NOT SORTED" in out
    assert "KEEPS COMING UP" in out


# --- schedule ----------------------------------------------------------------

def test_schedule_prints_a_command_without_running_anything():
    code, out = _run(["schedule", "--at", "07:30"], _store())
    assert code == 0
    assert "To schedule it" in out and "07:30" in out
    assert "-m jarvis brief" in out


def test_schedule_rejects_a_bad_time():
    code, out = _run(["schedule", "--at", "nope"], _store())
    assert code == 1 and "Error" in out
