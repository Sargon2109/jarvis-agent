"""Tests for the Canvas integration. Fully offline — a fake opener stands in for
the network, so pagination, auth, normalization, and the idempotent sync are all
exercised without a real Canvas."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from jarvis.canvas import (
    CanvasClient,
    CanvasConfig,
    CanvasError,
    _next_link,
    _to_local_date,
    sync_to_store,
)
from jarvis.storage import JSONStore


def _store() -> JSONStore:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-canvas-"))
    return JSONStore(tmp / "plate.json")


def _config() -> CanvasConfig:
    return CanvasConfig(base_url="https://school.test", token="tok")


class _FakeCanvas:
    """A canned Canvas: maps URL-substrings to (status, headers, json body)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        # Most specific (longest) matching needle wins, so "courses?page=2"
        # beats the substring "courses?" regardless of dict order.
        best = max(
            (n for n in self.routes if n in url),
            key=len,
            default=None,
        )
        if best is not None:
            status, resp_headers, body = self.routes[best]
            return status, resp_headers, json.dumps(body).encode("utf-8")
        return 404, {}, b'{"errors": "not found"}'


# --- config ------------------------------------------------------------------

def test_config_from_env_requires_both_values(monkeypatch=None):
    import os
    os.environ.pop("JARVIS_CANVAS_URL", None)
    os.environ.pop("JARVIS_CANVAS_TOKEN", None)
    raised = False
    try:
        CanvasConfig.from_env()
    except CanvasError:
        raised = True
    assert raised


def test_config_from_env_adds_scheme_and_strips_slash():
    import os
    os.environ["JARVIS_CANVAS_URL"] = "school.instructure.com/"
    os.environ["JARVIS_CANVAS_TOKEN"] = "abc"
    try:
        config = CanvasConfig.from_env()
        assert config.base_url == "https://school.instructure.com"
        assert config.token == "abc"
    finally:
        del os.environ["JARVIS_CANVAS_URL"]
        del os.environ["JARVIS_CANVAS_TOKEN"]


# --- small helpers -----------------------------------------------------------

def test_next_link_parsing():
    header = '<https://x/a?page=2>; rel="next", <https://x/a?page=9>; rel="last"'
    assert _next_link(header) == "https://x/a?page=2"
    assert _next_link('<https://x/a?page=9>; rel="last"') is None
    assert _next_link("") is None


def test_utc_due_becomes_a_local_date():
    # A UTC timestamp normalizes to a YYYY-MM-DD (exact day depends on local tz,
    # so just assert it parses to the right shape and near date).
    assert _to_local_date(None) is None
    assert _to_local_date("garbage") is None
    got = _to_local_date("2026-08-25T15:00:00Z")
    assert got.startswith("2026-08-2")


# --- client: auth, pagination, errors ----------------------------------------

def test_client_sends_bearer_token_and_follows_pagination():
    routes = {
        "courses?": (200, {"Link": '<https://school.test/api/v1/courses?page=2>; rel="next"'},
                     [{"id": 1, "name": "Econ"}]),
        "courses?page=2": (200, {}, [{"id": 2, "name": "History"}]),
    }
    fake = _FakeCanvas(routes)
    client = CanvasClient(_config(), opener=fake)
    courses = client.active_courses()
    assert [c["name"] for c in courses] == ["Econ", "History"]
    # The token rode along on every call.
    assert all(h["Authorization"] == "Bearer tok" for _, h in fake.calls)


def test_client_maps_401_to_a_helpful_error():
    fake = _FakeCanvas({"courses": (401, {}, {"errors": "unauthorized"})})
    client = CanvasClient(_config(), opener=fake)
    raised = ""
    try:
        client.active_courses()
    except CanvasError as exc:
        raised = str(exc)
    assert "token" in raised.lower()


# --- upcoming assignments: windowing -----------------------------------------

def _client_with(courses, assignments_by_course):
    routes = {"courses?": (200, {}, courses)}
    for cid, assignments in assignments_by_course.items():
        routes[f"courses/{cid}/assignments"] = (200, {}, assignments)
    return CanvasClient(_config(), opener=_FakeCanvas(routes))


def test_upcoming_filters_undated_past_and_far_future():
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    client = _client_with(
        [{"id": 1, "name": "Econ"}],
        {1: [
            {"id": 10, "name": "Due soon", "due_at": "2026-08-25T17:00:00Z",
             "html_url": "https://school.test/a/10"},
            {"id": 11, "name": "No date", "due_at": None,
             "html_url": "https://school.test/a/11"},
            {"id": 12, "name": "Already past", "due_at": "2026-08-01T17:00:00Z",
             "html_url": "https://school.test/a/12"},
            {"id": 13, "name": "Far future", "due_at": "2027-01-01T17:00:00Z",
             "html_url": "https://school.test/a/13"},
        ]},
    )
    upcoming = client.upcoming_assignments(within_days=60, now=now)
    assert [a.title for a in upcoming] == ["Due soon"]
    assert upcoming[0].course == "Econ"
    assert upcoming[0].due == "2026-08-25"


# --- sync: idempotent import -------------------------------------------------

def test_sync_imports_then_dedupes_on_rerun():
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    store = _store()
    client = _client_with(
        [{"id": 1, "name": "Econ"}],
        {1: [
            {"id": 10, "name": "Problem set 3", "due_at": "2026-08-25T17:00:00Z",
             "html_url": "https://school.test/a/10"},
            {"id": 11, "name": "Essay", "due_at": "2026-09-01T17:00:00Z",
             "html_url": "https://school.test/a/11"},
        ]},
    )

    first = sync_to_store(store, client, now=now)
    assert len(first.added) == 2 and first.skipped == 0
    items = store.list(domain="homework")
    assert len(items) == 2
    assert all(i.kind == "reminder" for i in items)
    assert {i.due for i in items} == {"2026-08-25", "2026-09-01"}

    # Re-running the same sync adds nothing and skips both.
    second = sync_to_store(store, client, now=now)
    assert second.added == [] and second.skipped == 2
    assert len(store.list(domain="homework")) == 2


def test_dry_run_reports_without_writing():
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    store = _store()
    client = _client_with(
        [{"id": 1, "name": "Econ"}],
        {1: [{"id": 10, "name": "Quiz", "due_at": "2026-08-25T17:00:00Z",
              "html_url": "https://school.test/a/10"}]},
    )
    result = sync_to_store(store, client, dry_run=True, now=now)
    assert len(result.added) == 1
    assert store.all() == []          # nothing persisted


def test_only_new_assignments_are_added_on_incremental_sync():
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    store = _store()
    routes_v1 = {1: [{"id": 10, "name": "PSet", "due_at": "2026-08-25T17:00:00Z",
                      "html_url": "https://school.test/a/10"}]}
    client1 = _client_with([{"id": 1, "name": "Econ"}], routes_v1)
    sync_to_store(store, client1, now=now)

    # A new assignment appears later.
    routes_v2 = {1: routes_v1[1] + [
        {"id": 11, "name": "Final paper", "due_at": "2026-09-10T17:00:00Z",
         "html_url": "https://school.test/a/11"}]}
    client2 = _client_with([{"id": 1, "name": "Econ"}], routes_v2)
    result = sync_to_store(store, client2, now=now)
    assert [a.title for a in result.added] == ["Final paper"]
    assert result.skipped == 1
    assert len(store.list(domain="homework")) == 2
