"""Generating the command that runs your morning briefing.

Jarvis does not install anything on your machine. Registering a scheduled job is
a change to your system, not to this project, so this module *composes the exact
command* and hands it to you to run — you stay the one who decides whether
something starts running unattended every morning.

Both platforms are supported because the same store travels between machines:
Windows uses Task Scheduler (``schtasks``), macOS and Linux use ``cron``.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Default time for the morning briefing, 24-hour ``HH:MM``.
DEFAULT_TIME = "07:00"

#: The name the scheduled job is registered under on Windows.
TASK_NAME = "JarvisMorningBrief"


def parse_time(value: str) -> tuple[int, int]:
    """Validate an ``HH:MM`` string and return (hour, minute)."""
    text = (value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"time must look like HH:MM, got {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"time must look like HH:MM, got {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range, got {value!r}")
    return hour, minute


@dataclass
class SchedulePlan:
    """A ready-to-run scheduling command, plus how to undo it."""

    platform: str          # "windows" | "unix"
    at: str                # HH:MM
    command: str           # the briefing command itself
    install: str           # the command that registers it
    remove: str            # the command that unregisters it
    note: str = ""

    def render(self) -> str:
        lines = [
            f"Morning briefing at {self.at} ({self.platform}).",
            "",
            "The command that will run:",
            f"  {self.command}",
            "",
            "To schedule it, run this yourself:",
            f"  {self.install}",
            "",
            "To undo:",
            f"  {self.remove}",
        ]
        if self.note:
            lines += ["", self.note]
        return "\n".join(lines)


def briefing_command(
    python: Optional[str] = None, project_root: Optional[Path] = None
) -> str:
    """The command that produces a briefing, with absolute paths.

    Absolute paths matter: a scheduler runs the job from an unpredictable working
    directory, so a relative ``python -m jarvis`` would simply fail at 7am with
    nobody watching.
    """
    python = python or sys.executable
    root = Path(project_root or Path(__file__).resolve().parent.parent)
    if sys.platform == "win32":
        return f'cd /d "{root}" && "{python}" -m jarvis brief'
    return f"cd {shlex.quote(str(root))} && {shlex.quote(python)} -m jarvis brief"


def build_plan(
    at: str = DEFAULT_TIME,
    *,
    platform: Optional[str] = None,
    python: Optional[str] = None,
    project_root: Optional[Path] = None,
) -> SchedulePlan:
    """Compose the scheduling commands for this machine (or a named platform)."""
    hour, minute = parse_time(at)
    at = f"{hour:02d}:{minute:02d}"
    target = platform or ("windows" if sys.platform == "win32" else "unix")
    command = briefing_command(python=python, project_root=project_root)

    if target == "windows":
        install = (
            f'schtasks /Create /SC DAILY /TN "{TASK_NAME}" '
            f'/TR "cmd /c {command}" /ST {at}'
        )
        remove = f'schtasks /Delete /TN "{TASK_NAME}" /F'
        note = (
            "Task Scheduler runs this whether or not a terminal is open. "
            "Output goes nowhere by default — append `> jarvis-brief.txt` to the "
            "command if you want to read it later."
        )
    else:
        install = (
            f'(crontab -l 2>/dev/null; echo "{minute} {hour} * * * {command}") '
            f"| crontab -"
        )
        remove = "crontab -e   # then delete the jarvis brief line"
        note = (
            "On macOS your terminal app may need Full Disk Access for cron jobs to "
            "read files in protected folders."
        )

    return SchedulePlan(
        platform=target, at=at, command=command, install=install, remove=remove, note=note
    )


def is_scheduled() -> Optional[bool]:
    """Whether the morning job appears to be registered.

    Returns None when it can't be determined — never guesses, because reporting
    "not scheduled" for a job that is in fact running would be worse than
    admitting ignorance.
    """
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["schtasks", "/Query", "/TN", TASK_NAME],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return False
        return "jarvis brief" in result.stdout
    except (OSError, subprocess.SubprocessError):
        return None
