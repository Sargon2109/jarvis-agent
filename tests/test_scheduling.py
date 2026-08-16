"""Tests for schedule-command generation. Nothing here registers anything."""

from pathlib import Path

from jarvis.scheduling import TASK_NAME, build_plan, briefing_command, parse_time


def test_parse_time_accepts_valid_times():
    assert parse_time("07:00") == (7, 0)
    assert parse_time("23:59") == (23, 59)
    assert parse_time(" 06:30 ") == (6, 30)


def test_parse_time_rejects_bad_input():
    for bad in ("7", "25:00", "07:60", "seven", "", "7:00:00"):
        raised = False
        try:
            parse_time(bad)
        except ValueError:
            raised = True
        assert raised, f"{bad!r} should be rejected"


def test_briefing_command_uses_absolute_paths():
    """A scheduler runs from an unpredictable directory; relative paths would fail."""
    root = Path("/proj")
    command = briefing_command(python="/usr/bin/python3", project_root=root)
    assert "/usr/bin/python3" in command
    assert str(root) in command   # str() so the check holds on Windows too
    assert "-m jarvis brief" in command


def test_windows_plan_uses_schtasks_and_normalizes_time():
    plan = build_plan("7:5", platform="windows", python="py.exe", project_root=Path("/p"))
    assert plan.at == "07:05"
    assert "schtasks /Create" in plan.install
    assert TASK_NAME in plan.install
    assert "/ST 07:05" in plan.install
    assert "schtasks /Delete" in plan.remove


def test_unix_plan_uses_cron_with_minute_then_hour():
    plan = build_plan("07:30", platform="unix", python="python3", project_root=Path("/p"))
    assert "crontab" in plan.install
    assert "30 7 * * *" in plan.install   # cron order is minute hour
    assert "crontab -e" in plan.remove


def test_plan_rejects_a_bad_time():
    raised = False
    try:
        build_plan("nope", platform="unix")
    except ValueError:
        raised = True
    assert raised


def test_render_shows_command_install_and_undo():
    text = build_plan("07:00", platform="unix", python="python3", project_root=Path("/p")).render()
    assert "The command that will run" in text
    assert "To schedule it" in text
    assert "To undo" in text


def test_both_platforms_produce_distinct_plans():
    win = build_plan("07:00", platform="windows", python="p", project_root=Path("/p"))
    nix = build_plan("07:00", platform="unix", python="p", project_root=Path("/p"))
    assert win.install != nix.install
    assert win.platform == "windows" and nix.platform == "unix"
