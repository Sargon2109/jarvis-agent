"""Native notifications — the tap on the shoulder.

The briefing answers "what would I want to know?" but only when you ask. This
module pushes the urgent slice of it — what's overdue and what's due today — as
real desktop notifications, so a deadline can interrupt you instead of waiting
politely in a terminal you didn't open.

Design choices:

* **Same source of truth.** Notifications are computed from the same agenda the
  CLI and the desk render; there is no second notion of "due".
* **Once per item per day.** A scheduled job may fire hourly, but nagging turns
  notifications into noise that gets swiped away unread. A small state file
  remembers what was already sent today; a new day resets it.
* **Stdlib only, injectable sender.** macOS uses ``osascript`` (present on every
  Mac), Linux uses ``notify-send``, anywhere else prints. The actual sender is
  a callable that tests replace, so everything here is testable offline.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Optional

from .agenda import build_agenda
from .models import Item

#: Fallback location: <repo>/data/notify-state.json (data/ is gitignored).
DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "notify-state.json"

#: Env var to override where the dedupe state lives.
STATE_PATH_ENV = "JARVIS_NOTIFY_STATE_PATH"


def default_state_path() -> Path:
    override = os.environ.get(STATE_PATH_ENV)
    return Path(override) if override else DEFAULT_STATE_PATH


@dataclass
class Notification:
    """One desktop notification, ready to send."""

    item_id: str
    title: str      # e.g. "Overdue" / "Due today"
    body: str       # the item's own title


def build_notifications(
    items: Iterable[Item], *, today: Optional[date] = None
) -> list[Notification]:
    """The urgent slice of the agenda as notifications: overdue, then today.

    Upcoming items are deliberately excluded — a notification is an
    interruption, and interrupting for something due next week teaches the user
    to ignore the ones that matter.
    """
    agenda = build_agenda(items, today=today)
    notifications: list[Notification] = []
    for item in agenda.overdue:
        notifications.append(
            Notification(item_id=item.id, title=f"Overdue ({item.due})", body=item.title)
        )
    for item in agenda.today:
        notifications.append(
            Notification(item_id=item.id, title="Due today", body=item.title)
        )
    return notifications


class NotifyState:
    """Remembers which items were already announced today.

    One tiny JSON document: ``{"date": "YYYY-MM-DD", "sent": [ids...]}``. A
    different date on load means a new day — everything is fair game again.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else default_state_path()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"date": "", "sent": []}
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"date": "", "sent": []}   # corrupt state = notify again, never crash
        if not isinstance(doc, dict):
            return {"date": "", "sent": []}
        return {"date": str(doc.get("date", "")), "sent": list(doc.get("sent", []))}

    def sent_today(self, today: date) -> set[str]:
        doc = self._load()
        return set(doc["sent"]) if doc["date"] == today.isoformat() else set()

    def mark_sent(self, today: date, item_ids: Iterable[str]) -> None:
        already = self.sent_today(today)
        already.update(item_ids)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"date": today.isoformat(), "sent": sorted(already)}) + "\n",
            encoding="utf-8",
        )


# --- senders -----------------------------------------------------------------

def _osascript_args(notification: Notification) -> list[str]:
    """The macOS notification command. Values are embedded in an AppleScript
    string literal, so quotes and backslashes must be escaped."""

    def escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{escape(notification.body)}" '
        f'with title "JARVIS" subtitle "{escape(notification.title)}"'
    )
    return ["osascript", "-e", script]


def _default_sender(notification: Notification) -> bool:
    """Send one notification via the platform's native mechanism."""
    if sys.platform == "darwin":
        args = _osascript_args(notification)
    elif sys.platform.startswith("linux"):
        args = ["notify-send", f"JARVIS — {notification.title}", notification.body]
    else:
        print(f"[notify] {notification.title}: {notification.body}")
        return True
    try:
        result = subprocess.run(args, capture_output=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError):
        # ValueError: a NUL byte in the title (JSON can encode one) makes
        # subprocess reject the argument. One unsendable notification must
        # never take down a scheduled run.
        return False


@dataclass
class NotifyResult:
    """What a notify run did, for the CLI to report."""

    sent: list[Notification] = field(default_factory=list)
    deduped: int = 0          # already announced today
    failed: int = 0

    def summary(self) -> str:
        if not self.sent and not self.deduped:
            return "Nothing due — no notifications to send."
        parts = [f"Sent {len(self.sent)} notification(s)"]
        if self.deduped:
            parts.append(f"{self.deduped} already announced today")
        if self.failed:
            parts.append(f"{self.failed} failed to display")
        lines = [", ".join(parts) + "."]
        lines += [f"  {n.title}: {n.body}" for n in self.sent]
        return "\n".join(lines)


def run_notify(
    items: Iterable[Item],
    *,
    state: Optional[NotifyState] = None,
    sender: Optional[Callable[[Notification], bool]] = None,
    today: Optional[date] = None,
    dry_run: bool = False,
) -> NotifyResult:
    """Send today's due/overdue notifications, once per item per day.

    ``dry_run`` reports what would be sent without sending or recording
    anything — for checking what a scheduled run is about to do.
    """
    state = state or NotifyState()
    sender = sender or _default_sender
    today = today or date.today()

    pending = build_notifications(items, today=today)
    already = state.sent_today(today)

    result = NotifyResult()
    for notification in pending:
        if notification.item_id in already:
            result.deduped += 1
            continue
        if dry_run:
            result.sent.append(notification)
            continue
        if sender(notification):
            result.sent.append(notification)
        else:
            result.failed += 1

    if result.sent and not dry_run:
        state.mark_sent(today, [n.item_id for n in result.sent])
    return result
