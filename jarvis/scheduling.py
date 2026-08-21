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

#: launchd label / Windows task name for the recurring notification job.
NOTIFY_LABEL = "com.jarvis.notify"
NOTIFY_TASK_NAME = "JarvisNotify"


def _xml_escape(text: str) -> str:
    """Escape a value for inclusion in plist XML text."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


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
    python: Optional[str] = None,
    project_root: Optional[Path] = None,
    target: Optional[str] = None,
) -> str:
    """The command that produces a briefing, with absolute paths.

    Absolute paths matter: a scheduler runs the job from an unpredictable working
    directory, so a relative ``python -m jarvis`` would simply fail at 7am with
    nobody watching. ``target`` selects the shell quoting; it defaults to this
    machine but must be explicit when composing a plan for another OS.
    """
    return jarvis_command("brief", python=python, project_root=project_root, target=target)


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


def _this_platform() -> str:
    """This machine's plan target."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "unix"


def jarvis_command(
    subcommand: str,
    python: Optional[str] = None,
    project_root: Optional[Path] = None,
    target: Optional[str] = None,
) -> str:
    """Any ``jarvis <subcommand>`` as an absolute-path shell command.

    Quoting follows ``target``, not the running machine — otherwise a plan
    composed on a Mac for Windows would carry POSIX quoting that cmd.exe can't
    parse (and vice versa).
    """
    python = python or sys.executable
    root = Path(project_root or Path(__file__).resolve().parent.parent)
    if (target or _this_platform()) == "windows":
        return f'cd /d "{root}" && "{python}" -m jarvis {subcommand}'
    return f"cd {shlex.quote(str(root))} && {shlex.quote(python)} -m jarvis {subcommand}"


def _schtasks_tr(command: str) -> str:
    """Quote a command for schtasks ``/TR``.

    The whole value is wrapped in double quotes, so any inner quote (which a
    path containing a space always produces) must be escaped as ``\"`` or
    schtasks rejects the command.
    """
    return command.replace('"', '\\"')


def _launchd_plist(
    python: str, project_root: Path, *, every_minutes: int
) -> str:
    """Build the plist. Paths are XML-escaped: an ``&`` in a directory name
    would otherwise produce a document launchd can't parse."""
    python = _xml_escape(python)
    project_root_text = _xml_escape(str(project_root))
    """A launchd user-agent plist for the notify job.

    launchd (not cron) because macOS grants notification permission to launchd
    user agents reliably, and it re-fires missed intervals after sleep.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{NOTIFY_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>jarvis</string>
        <string>notify</string>
    </array>
    <key>WorkingDirectory</key><string>{project_root_text}</string>
    <key>StartInterval</key><integer>{every_minutes * 60}</integer>
    <key>RunAtLoad</key><true/>
</dict>
</plist>"""


def build_notify_plan(
    *,
    every_minutes: int = 60,
    platform: Optional[str] = None,
    python: Optional[str] = None,
    project_root: Optional[Path] = None,
) -> SchedulePlan:
    """Compose the commands that run ``jarvis notify`` on a repeat.

    Same philosophy as the briefing plan: everything is printed for the user to
    run themselves — registering a background job is a change to the machine,
    and that stays their decision.
    """
    if every_minutes < 1:
        raise ValueError(f"interval must be at least 1 minute, got {every_minutes}")
    python = python or sys.executable
    root = Path(project_root or Path(__file__).resolve().parent.parent)
    target = platform or _this_platform()
    command = jarvis_command("notify", python=python, project_root=root, target=target)

    if target == "macos":
        plist_path = f"~/Library/LaunchAgents/{NOTIFY_LABEL}.plist"
        plist = _launchd_plist(python, root, every_minutes=every_minutes)
        install = (
            f"mkdir -p ~/Library/LaunchAgents && cat > {plist_path} <<'PLIST'"
            + "\n" + plist + "\nPLIST\n"
            + f"launchctl load {plist_path}"
        )
        remove = f"launchctl unload {plist_path} && rm {plist_path}"
        note = (
            "macOS will ask once whether to allow notifications from Script "
            "Editor/osascript - allow it, or the alerts are silently dropped."
        )
    elif target == "windows":
        if every_minutes > 1439:
            # schtasks /SC MINUTE /MO tops out below a day; anything longer is
            # a daily task, so say so instead of composing a rejected command.
            raise ValueError(
                "Windows supports intervals up to 1439 minutes; for daily runs "
                "use `jarvis schedule --job brief` or schtasks /SC DAILY."
            )
        install = (
            f'schtasks /Create /SC MINUTE /MO {every_minutes} '
            f'/TN "{NOTIFY_TASK_NAME}" /TR "{_schtasks_tr("cmd /c " + command)}"'
        )
        remove = f'schtasks /Delete /TN "{NOTIFY_TASK_NAME}" /F'
        note = "Windows toast output depends on the console session; test with `jarvis notify` first."
    else:
        notes = ["Requires notify-send (libnotify) for desktop notifications."]
        if every_minutes < 60:
            cron_expr = f"*/{every_minutes} * * * *"
            if 60 % every_minutes:
                # */7 restarts each hour, so the gap across the boundary is short.
                notes.append(
                    f"cron restarts the */{every_minutes} cycle every hour, so the "
                    "interval is uneven at the top of each hour. Use a divisor of "
                    "60 (5, 10, 15, 20, 30) for an even cadence."
                )
        elif every_minutes % 60 == 0:
            cron_expr = f"0 */{every_minutes // 60} * * *"
        else:
            # cron can't express e.g. "every 90 minutes" — round and be honest.
            hours = max(1, round(every_minutes / 60))
            cron_expr = f"0 */{hours} * * *"
            notes.append(
                f"cron can't express a {every_minutes}-minute interval; this runs "
                f"every {hours}h instead."
            )
        install = f'(crontab -l 2>/dev/null; echo "{cron_expr} {command}") | crontab -'
        remove = "crontab -e   # then delete the jarvis notify line"
        note = " ".join(notes)

    return SchedulePlan(
        platform=target,
        at=f"every {every_minutes} min",
        command=command,
        install=install,
        remove=remove,
        note=note,
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
