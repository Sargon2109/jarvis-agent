"""Tests for the command desk's API and HTTP shell. No API calls.

The chat endpoint is the only part that needs a model, so it isn't exercised
here; everything else — state, mutations, routing, and the loopback default —
runs fully offline against temp-file backends.
"""

import json
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from jarvis.agents import RESERVED_NAMES
from jarvis.dumps import DumpLog
from jarvis.ledger import CostLedger
from jarvis.registry import AgentRegistry
from jarvis.server import WEB_DIR, JarvisAPI, build_handler
from jarvis.storage import JSONStore


def _api() -> JarvisAPI:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-web-"))
    (tmp / "scratch").mkdir()
    return JarvisAPI(
        JSONStore(tmp / "plate.json"),
        AgentRegistry(tmp / "agents.json", reserved=RESERVED_NAMES),
        DumpLog(tmp / "dumps.jsonl"),
        CostLedger(tmp / "costs.jsonl"),
        scratch_dir=tmp / "scratch",
    )


# --- state -------------------------------------------------------------------

def test_state_on_a_fresh_system_is_quiet_but_complete():
    state = _api().state()
    assert state["items"] == []
    assert state["stats"]["quiet"] is True
    # The roster is available before anything has been captured.
    assert {a["name"] for a in state["agents"]} >= {"generalist", "club", "writer"}


def test_state_buckets_items_into_the_agenda():
    api = _api()
    api.store.add("overdue thing", kind="task", domain="club", due="2000-01-01")
    api.store.add("no date", kind="task", domain="club")
    state = api.state()

    assert len(state["agenda"]["overdue"]) == 1
    assert state["stats"]["overdue"] == 1
    assert state["stats"]["active"] == 2
    # Derived fields the front end renders are present.
    assert state["items"][0]["age_days"] is not None


def test_state_reports_promotion_candidates_and_unsorted_dumps():
    api = _api()
    for n in range(3):
        api.store.add(f"debate {n}", kind="task", domain="debate")
    api.log.append("something I never sorted", source="cli")

    state = api.state()
    assert [c["domain"] for c in state["candidates"]] == ["debate"]
    assert state["candidates"][0]["count"] == 3
    assert len(state["unsorted_dumps"]) == 1


def test_custom_agent_shows_its_domain_and_origin():
    api = _api()
    api.registry.add("debate", "Debate prep.", "You are the debate specialist.", domain="debate")
    agents = {a["name"]: a for a in api.state()["agents"]}
    assert agents["debate"]["origin"] == "custom"
    assert agents["debate"]["domain"] == "debate"
    assert agents["club"]["origin"] == "built-in"


# --- mutations ---------------------------------------------------------------

def test_add_complete_reopen_and_remove_round_trip():
    api = _api()
    item = api.add_item({"text": "book the room", "kind": "task", "domain": "club"})["item"]
    assert item["status"] == "inbox"

    assert api.complete_item(item["id"])["item"]["status"] == "done"
    assert api.reopen_item(item["id"])["item"]["status"] == "active"
    assert api.remove_item(item["id"])["removed"] == item["id"]
    assert api.state()["items"] == []


def test_add_item_rejects_empty_text():
    raised = False
    try:
        _api().add_item({"text": "   "})
    except ValueError:
        raised = True
    assert raised


def test_mutating_an_unknown_id_raises_lookup_error():
    api = _api()
    for call in (api.complete_item, api.reopen_item, api.remove_item):
        raised = False
        try:
            call("nope")
        except LookupError:
            raised = True
        assert raised, call.__name__


def test_save_dump_is_recorded_verbatim():
    api = _api()
    api.save_dump("  raw thoughts  ")
    assert api.log.all()[0].text == "raw thoughts"


# --- chat guards (no model involved) -----------------------------------------

def test_empty_chat_message_errors_without_running_anything():
    api = _api()
    events = []
    api.stream_chat("   ", events.append)
    assert [e["type"] for e in events] == ["error"]
    assert api.log.all() == []  # nothing recorded for an empty prompt


def test_a_second_chat_is_refused_while_one_is_running():
    api = _api()
    api._chat_lock.acquire()
    try:
        events = []
        api.stream_chat("hello", events.append)
    finally:
        api._chat_lock.release()
    assert events[0]["type"] == "error"
    assert "still working" in events[0]["message"]


# --- event translation -------------------------------------------------------

class _Block:
    """Stand-in for the SDK's ToolUseBlock."""

    def __init__(self, id, name, input):
        self.id, self.name, self.input = id, name, input


def test_delegation_is_remembered_so_subagent_output_is_attributed():
    api = _api()
    delegations = {}
    event = api._tool_event(
        _Block("t1", "Task", {"subagent_type": "club", "description": "charter"}),
        delegations, None,
    )
    assert event["type"] == "delegate" and event["agent"] == "club"
    assert delegations["t1"] == "club"


def test_store_tools_are_unprefixed_and_writes_are_flagged():
    api = _api()
    capture = api._tool_event(
        _Block("t2", "mcp__jarvis_store__capture_thought", {"title": "x", "due": "2026-01-01"}),
        {}, "club",
    )
    assert capture["tool"] == "capture_thought"
    assert capture["scope"] == "store"
    assert capture["mutates"] is True
    assert capture["agent"] == "club"
    assert "2026-01-01" in capture["detail"]

    # Reads must not trigger a plate refresh.
    assert api._tool_event(
        _Block("t3", "mcp__jarvis_store__list_plate", {}), {}, None
    )["mutates"] is False


def test_file_tools_are_reported_with_their_path():
    event = _api()._tool_event(
        _Block("t4", "Write", {"file_path": "scratch/charter.md"}), {}, "club"
    )
    assert event["scope"] == "file"
    assert event["detail"] == "scratch/charter.md"


# --- HTTP shell --------------------------------------------------------------

def _server(api: JarvisAPI):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(api))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def _post(url, payload=None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_http_serves_the_page_and_the_state_endpoint():
    api = _api()
    api.store.add("a thing", kind="task", domain="club")
    httpd, base = _server(api)
    try:
        status, body = _get(base + "/")
        assert status == 200 and b"JARVIS" in body

        status, body = _get(base + "/api/state")
        assert status == 200
        assert json.loads(body)["stats"]["active"] == 1
    finally:
        httpd.shutdown()


def test_http_mutations_and_error_codes():
    api = _api()
    httpd, base = _server(api)
    try:
        _, created = _post(base + "/api/items", {"text": "from the desk", "domain": "club"})
        item_id = created["item"]["id"]

        _, done = _post(f"{base}/api/items/{item_id}/done")
        assert done["item"]["status"] == "done"

        _, removed = _post(f"{base}/api/items/{item_id}/remove")
        assert removed["removed"] == item_id

        # A missing item is a 404, a bad body is a 400, an unknown route is a 404.
        for path, payload, expected in (
            ("/api/items/nope/done", {}, 404),
            ("/api/items", {"text": ""}, 400),
            ("/api/nothing", {}, 404),
        ):
            code = None
            try:
                _post(base + path, payload)
            except urllib.error.HTTPError as exc:
                code = exc.code
            assert code == expected, path
    finally:
        httpd.shutdown()


def test_the_front_end_file_ships_with_the_package():
    assert (WEB_DIR / "index.html").exists()


# --- scratch browser ---------------------------------------------------------

def test_scratch_lists_and_reads_drafts():
    api = _api()
    (api.scratch_dir / "plan.md").write_text("# The Plan", encoding="utf-8")
    (api.scratch_dir / ".hidden").write_text("nope", encoding="utf-8")

    files = api.scratch_files()["files"]
    assert [f["name"] for f in files] == ["plan.md"]  # dotfiles are not drafts

    got = api.scratch_file("plan.md")
    assert got["content"] == "# The Plan"


def test_scratch_rejects_path_escapes():
    api = _api()
    (api.scratch_dir / "real.md").write_text("ok", encoding="utf-8")
    for name in ("../secrets.txt", "a/b.md", "..", ".env", ""):
        raised = False
        try:
            api.scratch_file(name)
        except LookupError:
            raised = True
        assert raised, repr(name)


# --- session -----------------------------------------------------------------

def test_reset_session_clears_continuity():
    api = _api()
    api._session_id = "sess-123"
    assert api.state()["session"]["active"] is True
    assert api.reset_session() == {"session": {"active": False}}
    assert api._session_id is None
    assert api.state()["session"]["active"] is False


def test_reset_session_is_refused_during_a_run():
    api = _api()
    api._session_id = "sess-live"
    api._chat_lock.acquire()          # simulate a turn in flight
    try:
        result = api.reset_session()
    finally:
        api._chat_lock.release()
    assert result["busy"] is True
    assert api._session_id == "sess-live"   # the running turn keeps its session


# --- ledger in state ---------------------------------------------------------

def test_state_reports_ledger_totals():
    api = _api()
    api.ledger.append(0.25)
    api.ledger.append(0.50)
    costs = api.state()["costs"]
    assert abs(costs["month"] - 0.75) < 1e-9
    assert abs(costs["total"] - 0.75) < 1e-9


# --- fake-SDK end-to-end chat turn -------------------------------------------

def test_stream_chat_translates_a_full_sdk_turn(monkeypatch=None):
    """One offline chat turn through the real stream_chat/_arun pipeline.

    The SDK's query() is replaced with a canned message stream, so this pins
    the exact translation the frontend depends on — the thing an SDK upgrade
    would break first — without any API call.
    """
    import jarvis.server as server_mod
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
    )

    api = _api()

    fake_messages = [
        AssistantMessage(
            content=[
                TextBlock(text="Capturing that now."),
                ToolUseBlock(
                    id="t1",
                    name="mcp__jarvis_store__capture_thought",
                    input={"title": "book room", "kind": "task"},
                ),
                ToolUseBlock(
                    id="t2", name="Task",
                    input={"subagent_type": "club", "description": "charter"},
                ),
            ],
            model="fake",
        ),
        AssistantMessage(
            content=[TextBlock(text="Charter drafted.")],
            model="fake",
            parent_tool_use_id="t2",
        ),
        ResultMessage(
            subtype="success",
            duration_ms=1200,
            duration_api_ms=1000,
            is_error=False,
            num_turns=3,
            session_id="sess-abc",
            total_cost_usd=0.05,
        ),
    ]

    async def fake_query(prompt, options):
        assert options.resume is None  # first turn starts fresh
        for message in fake_messages:
            yield message

    original = server_mod.query
    server_mod.query = fake_query
    try:
        events = []
        api.stream_chat("book the club room", events.append)
    finally:
        server_mod.query = original

    kinds = [e["type"] for e in events]
    assert kinds == ["dump", "session", "text", "tool", "delegate", "text", "result", "end"]

    text0, tool, delegate, text1, result = events[2], events[3], events[4], events[5], events[6]
    assert text0["agent"] is None
    assert tool["tool"] == "capture_thought" and tool["mutates"] is True
    assert delegate["agent"] == "club"
    assert text1["agent"] == "club"          # attributed via parent_tool_use_id
    assert result["cost"] == 0.05

    # Side effects: session captured for continuity, cost in the ledger,
    # and the dump recorded before the model ran.
    assert api._session_id == "sess-abc"
    assert abs(api.ledger.total() - 0.05) < 1e-9
    assert api.log.all()[0].text == "book the club room"


def test_stream_chat_drops_a_stale_session_on_failure():
    import jarvis.server as server_mod

    api = _api()
    api._session_id = "sess-stale"

    async def exploding_query(prompt, options):
        assert options.resume == "sess-stale"
        raise RuntimeError("no such session")
        yield  # pragma: no cover - makes this an async generator

    original = server_mod.query
    server_mod.query = exploding_query
    try:
        events = []
        api.stream_chat("hello again", events.append)
    finally:
        server_mod.query = original

    assert any(e["type"] == "error" for e in events)
    assert api._session_id is None  # next turn starts fresh instead of wedging


# --- request authentication --------------------------------------------------

def _post_raw(url, payload=None, headers=None):
    data = json.dumps(payload or {}).encode()
    base = {"Content-Type": "application/json"}
    base.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=base)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_cross_origin_posts_are_refused():
    api = _api()
    httpd, base = _server(api)
    try:
        # An evil page's fetch() carries its own Origin.
        assert _post_raw(base + "/api/items", {"text": "evil"},
                         {"Origin": "https://evil.example"}) == 403
        # A cross-site simple request can't send application/json; a text/plain
        # smuggle must be refused even with no Origin check tripped.
        assert _post_raw(base + "/api/items", {"text": "evil"},
                         {"Content-Type": "text/plain"}) == 403
        # DNS rebinding: attacker's hostname resolving to 127.0.0.1.
        assert _post_raw(base + "/api/items", {"text": "evil"},
                         {"Host": "evil.example"}) == 403
        # And the same Host check guards reads, which is where data exfiltrates.
        req = urllib.request.Request(base + "/api/state", headers={"Host": "evil.example"})
        code = None
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 403
        # The legitimate desk still works.
        assert _post_raw(base + "/api/items", {"text": "fine"},
                         {"Origin": base}) == 200
        assert len(api.store.all()) == 1
    finally:
        httpd.shutdown()


def test_oversized_bodies_are_refused():
    api = _api()
    httpd, base = _server(api)
    try:
        big = "x" * 1_100_000
        # The server refuses without reading the flood, so the client sees
        # either the 400 or a reset mid-send. Both are refusal; the invariant
        # is that nothing oversized was ever recorded.
        try:
            code = _post_raw(base + "/api/dumps", {"text": big})
            assert code == 400
        except (urllib.error.URLError, ConnectionError, BrokenPipeError):
            pass
        assert api.log.all() == []
    finally:
        httpd.shutdown()


def test_http_scratch_routes():
    api = _api()
    (api.scratch_dir / "draft.md").write_text("# Draft", encoding="utf-8")
    httpd, base = _server(api)
    try:
        status, body = _get(base + "/api/scratch")
        assert status == 200
        assert json.loads(body)["files"][0]["name"] == "draft.md"

        status, body = _get(base + "/api/scratch/draft.md")
        assert json.loads(body)["content"] == "# Draft"

        code = None
        try:
            _get(base + "/api/scratch/..%2Fplate.json")
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 404
    finally:
        httpd.shutdown()


# --- system stats ------------------------------------------------------------

def test_system_stats_shape():
    stats = _api().system_stats()
    assert "load" in stats and "disk" in stats and "cpus" in stats
    if stats["disk"] is not None:
        assert stats["disk"]["total_gb"] > 0


def test_http_system_route():
    api = _api()
    httpd, base = _server(api)
    try:
        status, body = _get(base + "/api/system")
        assert status == 200
        assert "cpus" in json.loads(body)
    finally:
        httpd.shutdown()
