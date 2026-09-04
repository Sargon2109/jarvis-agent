"""Conversation continuity that survives restarts and days.

The desk resumes an SDK session so the conversation has a memory: "yes, the one
we discussed" means something. But the session id lived only in the server
process, so closing the desk — or restarting it after a code change — silently
ended the conversation. From the user's side Jarvis simply forgot, with no way
to tell that from a bug.

This keeps the id on disk. The Claude CLI already stores the conversation
itself, so a saved id is enough to pick the thread back up in a fresh process.

Two deliberate boundaries:

* **An idle limit, not a daily reset.** Remembering yesterday is the point.
  But a session resumed forever grows without bound — every turn re-sends the
  whole history, so cost climbs and the context window eventually fills. After
  ``MAX_IDLE_DAYS`` of silence the thread has served its purpose and a new one
  starts.
* **Dates are handled in the prompt, not here.** A conversation spanning days
  contains stale "today"s; the system prompt states the real date and tells the
  model to trust it over the history. Throwing away the conversation to avoid
  that would cost far more than it saves.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

#: Fallback location: <repo>/data/session.json (data/ is gitignored).
DEFAULT_SESSION_PATH = Path(__file__).resolve().parent.parent / "data" / "session.json"

#: Env var to relocate it, matching the other data paths.
SESSION_PATH_ENV = "JARVIS_SESSION_PATH"

#: How many days of silence end a conversation. Long enough that a weekend
#: doesn't break continuity, short enough that context stays affordable.
MAX_IDLE_DAYS = 7

#: Env override, so the tradeoff is a setting rather than a code edit.
MAX_IDLE_ENV = "JARVIS_SESSION_MAX_IDLE_DAYS"


def default_session_path() -> Path:
    override = os.environ.get(SESSION_PATH_ENV)
    return Path(override) if override else DEFAULT_SESSION_PATH


def max_idle_days() -> int:
    """The idle limit, from the environment when set and sane."""
    raw = os.environ.get(MAX_IDLE_ENV)
    if raw:
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            return MAX_IDLE_DAYS
        if parsed > 0:
            return parsed
    return MAX_IDLE_DAYS


@dataclass
class SessionRecord:
    """The conversation this desk is continuing."""

    session_id: str
    last_used: date

    def idle_days(self, today: date) -> int:
        return (today - self.last_used).days


class SessionStore:
    """Reads and writes the resumable session id."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else default_session_path()

    def load(self, *, today: Optional[date] = None) -> Optional[SessionRecord]:
        """The stored session, or None when there is none or it has gone stale.

        Every failure mode — missing file, bad JSON, a malformed date written by
        hand — degrades to "no session". Losing continuity is a disappointment;
        crashing the desk on startup because a state file got mangled is worse.
        """
        today = today or date.today()
        if not self.path.exists():
            return None
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        if not isinstance(doc, dict):
            return None
        session_id = doc.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None
        try:
            last_used = date.fromisoformat(str(doc.get("last_used", "")))
        except (ValueError, TypeError):
            return None

        record = SessionRecord(session_id=session_id, last_used=last_used)
        # A clock that moved backwards (timezone change, corrected system time)
        # gives a negative idle count. Treat that as current rather than stale.
        if record.idle_days(today) > max_idle_days():
            return None
        return record

    def save(self, session_id: str, *, today: Optional[date] = None) -> None:
        """Record the session, written atomically so a crash can't shred it."""
        today = today or date.today()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"session_id": session_id, "last_used": today.isoformat()}, indent=2
        )
        handle, temp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".session-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload + "\n")
            os.replace(temp_name, self.path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def clear(self) -> None:
        """Forget the conversation. Missing file is already the goal."""
        try:
            self.path.unlink()
        except (FileNotFoundError, OSError):
            pass
