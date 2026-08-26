"""Canvas integration — pull assignments and deadlines onto the plate.

Jarvis's job is that nothing you owe gets forgotten. Coursework deadlines are the
single biggest source of "things you owe", and they already live in one place:
your school's Canvas. This module reads them straight from the Canvas REST API
and captures each dated assignment as a ``homework`` reminder, so the plate and
the morning briefing surface them the same way everything else does.

Design choices that match the rest of the package:

* **Stdlib only.** The HTTP calls use :mod:`urllib.request`; no ``requests``
  dependency creeps in just for this.
* **Injectable transport.** :class:`CanvasClient` takes an ``opener`` callable,
  so every path — pagination, auth, error handling, normalization, and the sync
  into the store — is testable offline against a canned Canvas, with no network.
* **Idempotent sync.** Each assignment carries a stable Canvas URL; an item is
  imported only if that URL isn't already on the plate, so re-running the sync
  adds the new and skips the seen rather than duplicating your whole semester.
* **Secrets stay in the environment.** The base URL and token come from
  ``$JARVIS_CANVAS_URL`` / ``$JARVIS_CANVAS_TOKEN`` (put them in ``.env``),
  never committed and never passed on a command line where a shell would log them.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import DUE_FORMAT
from .storage import Store

#: Env vars holding the Canvas connection. Both are required to sync.
CANVAS_URL_ENV = "JARVIS_CANVAS_URL"
CANVAS_TOKEN_ENV = "JARVIS_CANVAS_TOKEN"

#: How far ahead to import by default. A whole term of past-due work isn't a
#: to-do list, and things centuries out are noise; this keeps the plate honest.
DEFAULT_WITHIN_DAYS = 60

#: The domain every Canvas item lands in, so the homework specialist owns them.
CANVAS_DOMAIN = "homework"

#: An opener returns (status_code, response_headers, body_bytes).
Opener = Callable[[str, dict], "tuple[int, dict, bytes]"]


class CanvasError(RuntimeError):
    """Raised when Canvas can't be reached or refuses the request."""


@dataclass
class CanvasConfig:
    """Where Canvas lives and how to authenticate to it."""

    base_url: str
    token: str

    @classmethod
    def from_env(cls) -> "CanvasConfig":
        """Build config from the environment, or raise a helpful CanvasError."""
        base = (os.environ.get(CANVAS_URL_ENV) or "").strip().rstrip("/")
        token = (os.environ.get(CANVAS_TOKEN_ENV) or "").strip()
        if not base or not token:
            raise CanvasError(
                "Canvas isn't configured. Set "
                f"{CANVAS_URL_ENV} (e.g. https://yourschool.instructure.com) and "
                f"{CANVAS_TOKEN_ENV} (a Canvas access token) in your .env."
            )
        if not base.startswith(("http://", "https://")):
            base = "https://" + base
        return cls(base_url=base, token=token)


@dataclass
class Assignment:
    """One Canvas assignment, normalized to what the store needs."""

    id: int
    course: str
    title: str
    due: Optional[str]        # local date YYYY-MM-DD, or None if undated
    url: str                  # unique, stable — the dedup key on re-sync

    def as_raw(self) -> str:
        """The item's ``raw`` text: human-readable and carrying the dedup URL."""
        return f"{self.title} — {self.course} ({self.url})"


def _default_opener(url: str, headers: dict) -> "tuple[int, dict, bytes]":
    """The real HTTP GET, via urllib. Never called in tests."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise CanvasError(f"Could not reach Canvas at {url}: {exc}") from exc


def _next_link(link_header: str) -> Optional[str]:
    """Extract the ``rel="next"`` URL from a Canvas Link header, if any."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip().lstrip("<").rstrip(">")
        if any('rel="next"' in s.replace(" ", "").replace("'", '"') for s in segments[1:]):
            return url
    return None


def _to_local_date(due_at: Optional[str]) -> Optional[str]:
    """Canvas gives a UTC timestamp; the agenda wants a local calendar date.

    Converting UTC -> local before taking the date matters: an 11pm-UTC deadline
    is a different *day* depending on the timezone, and the plate should show the
    day the user actually experiences.
    """
    if not due_at:
        return None
    try:
        parsed = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().date().strftime(DUE_FORMAT)


class CanvasClient:
    """A thin, paginated client for the Canvas REST API."""

    def __init__(self, config: CanvasConfig, opener: Optional[Opener] = None):
        self.config = config
        self._opener = opener or _default_opener

    def _get(self, path: str, params: Optional[dict] = None) -> list:
        """GET a paginated Canvas collection, following ``rel="next"`` to the end."""
        from urllib.parse import urlencode

        url: Optional[str]
        if path.startswith("http"):
            url = path
        else:
            query = urlencode({"per_page": 100, **(params or {})})
            url = f"{self.config.base_url}/api/v1/{path.lstrip('/')}?{query}"

        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Accept": "application/json",
        }
        results: list = []
        pages = 0
        while url and pages < 50:            # 50-page ceiling: a runaway guard
            status, resp_headers, body = self._opener(url, headers)
            if status == 401:
                raise CanvasError(
                    "Canvas rejected the token (401). Generate a fresh access "
                    f"token and update {CANVAS_TOKEN_ENV} in your .env."
                )
            if status >= 400:
                raise CanvasError(f"Canvas returned HTTP {status} for {url}")
            try:
                page = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise CanvasError(f"Canvas sent a malformed response: {exc}") from exc
            if isinstance(page, dict) and "errors" in page:
                raise CanvasError(f"Canvas error: {page['errors']}")
            results.extend(page if isinstance(page, list) else [page])
            # Case-insensitive Link header lookup (urllib normalizes, canned may not).
            link = next((v for k, v in resp_headers.items() if k.lower() == "link"), "")
            url = _next_link(link)
            pages += 1
        return results

    def active_courses(self) -> list[dict]:
        """Courses the user is currently enrolled in."""
        return self._get("courses", {"enrollment_state": "active"})

    def course_assignments(self, course_id: int) -> list[dict]:
        """Assignments for one course."""
        return self._get(f"courses/{course_id}/assignments")

    # --- course material (read-only) -----------------------------------------
    #
    # Everything below is GET-only by design. The token Canvas issues also
    # permits writes — submitting assignments, posting to discussions — and
    # Jarvis deliberately declines that power. An agent that can read your
    # coursework is a research assistant; one that can submit on your behalf is
    # a liability, and a misread deadline would turn into a real submission.

    def _get_optional(self, path: str, params: Optional[dict] = None) -> list:
        """GET a collection that a course may simply not use.

        Instructors disable tabs (Files, Pages, Modules) all the time, and
        Canvas answers 404 or 403 for a disabled tab. That means "this course
        doesn't use that feature", not "something went wrong" — so it degrades
        to an empty list instead of failing the agent's whole request.
        """
        try:
            return self._get(path, params)
        except CanvasError as exc:
            if "HTTP 404" in str(exc) or "HTTP 403" in str(exc):
                return []
            raise

    def course_files(self, course_id: int) -> list[dict]:
        """Files posted in a course (slides, readings, handouts)."""
        return self._get_optional(f"courses/{course_id}/files")

    def course_announcements(self, course_id: int) -> list[dict]:
        """Announcements, newest first."""
        return self._get_optional(
            "announcements", {"context_codes[]": f"course_{course_id}"}
        )

    def course_modules(self, course_id: int) -> list[dict]:
        """Modules with their items — the course's own sense of sequence."""
        return self._get_optional(
            f"courses/{course_id}/modules", {"include[]": "items"}
        )

    def course_pages(self, course_id: int) -> list[dict]:
        """Wiki pages (often where reading lists and policies actually live)."""
        return self._get_optional(f"courses/{course_id}/pages")

    def page_body(self, course_id: int, page_url: str) -> dict:
        """One page including its HTML body."""
        found = self._get(f"courses/{course_id}/pages/{page_url}")
        return found[0] if found else {}

    def course_syllabus(self, course_id: int) -> str:
        """The syllabus body as HTML ('' when the course has none)."""
        found = self._get(
            f"courses/{course_id}", {"include[]": "syllabus_body"}
        )
        if not found or not isinstance(found[0], dict):
            return ""
        return found[0].get("syllabus_body") or ""

    def download_file(self, file_url: str, dest: Path) -> Path:
        """Download one Canvas file to ``dest``.

        Canvas file URLs carry their own verifier, but sending the token too is
        harmless and covers files served straight from the API.
        """
        headers = {"Authorization": f"Bearer {self.config.token}"}
        status, _headers, body = self._opener(file_url, headers)
        if status == 401:
            raise CanvasError(
                "Canvas rejected the token (401) while downloading a file."
            )
        if status >= 400:
            raise CanvasError(f"Canvas returned HTTP {status} downloading {file_url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return dest

    def upcoming_assignments(
        self,
        *,
        within_days: int = DEFAULT_WITHIN_DAYS,
        now: Optional[datetime] = None,
    ) -> list[Assignment]:
        """Every dated assignment due from today through ``within_days`` ahead.

        Undated assignments are skipped — a reminder with no date can't help the
        agenda — and anything already past or beyond the window is dropped so the
        import is a live to-do list, not an archive.
        """
        now = now or datetime.now(timezone.utc)
        today = now.astimezone().date()
        assignments: list[Assignment] = []

        for course in self.active_courses():
            course_name = course.get("name") or f"Course {course.get('id')}"
            for raw in self.course_assignments(int(course["id"])):
                due = _to_local_date(raw.get("due_at"))
                if due is None:
                    continue
                due_date = datetime.strptime(due, DUE_FORMAT).date()
                if due_date < today or (due_date - today).days > within_days:
                    continue
                assignments.append(
                    Assignment(
                        id=int(raw.get("id", 0)),
                        course=course_name,
                        title=(raw.get("name") or "Untitled assignment").strip(),
                        due=due,
                        url=(raw.get("html_url") or "").strip(),
                    )
                )

        assignments.sort(key=lambda a: (a.due or "", a.course, a.title))
        return assignments


@dataclass
class SyncResult:
    """The outcome of a sync, for a human-readable report."""

    added: list[Assignment]
    skipped: int             # already on the plate
    found: int               # total in-window assignments Canvas returned

    def summary(self) -> str:
        if not self.found:
            return "No upcoming assignments found on Canvas in that window."
        lines = [
            f"Canvas: {self.found} upcoming, {len(self.added)} new, "
            f"{self.skipped} already tracked."
        ]
        for assignment in self.added:
            lines.append(f"  + [{assignment.due}] {assignment.title} ({assignment.course})")
        return "\n".join(lines)


def sync_to_store(
    store: Store,
    client: CanvasClient,
    *,
    within_days: int = DEFAULT_WITHIN_DAYS,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> SyncResult:
    """Import upcoming Canvas assignments as homework reminders. Idempotent.

    An assignment is *new* if its Canvas URL isn't already carried by some item
    on the plate; re-running only adds what appeared since last time. ``dry_run``
    reports what would happen without writing anything.
    """
    assignments = client.upcoming_assignments(within_days=within_days, now=now)

    existing = store.all()
    seen_urls = {
        assignment.url
        for assignment in assignments
        if assignment.url and any(assignment.url in (item.raw or "") for item in existing)
    }

    added: list[Assignment] = []
    skipped = 0
    for assignment in assignments:
        if assignment.url and assignment.url in seen_urls:
            skipped += 1
            continue
        if not dry_run:
            store.add(
                raw=assignment.as_raw(),
                title=assignment.title,
                kind="reminder",
                domain=CANVAS_DOMAIN,
                due=assignment.due,
            )
        added.append(assignment)

    return SyncResult(added=added, skipped=skipped, found=len(assignments))
