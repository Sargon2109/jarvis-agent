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


# --- the notify job ----------------------------------------------------------

def test_notify_plan_macos_uses_launchd():
    from jarvis.scheduling import build_notify_plan
    plan = build_notify_plan(platform="macos", python="/usr/bin/python3",
                             project_root="/tmp/jarvis", every_minutes=30)
    assert "com.jarvis.notify" in plan.install
    assert "<integer>1800</integer>" in plan.install     # 30 min in seconds
    assert "launchctl load" in plan.install
    assert "launchctl unload" in plan.remove
    # Printed, not executed: composing the plan changes nothing on the machine.


def test_notify_plan_unix_and_windows():
    from jarvis.scheduling import build_notify_plan
    unix = build_notify_plan(platform="unix", every_minutes=15)
    assert "*/15" in unix.install and "jarvis notify" in unix.command
    win = build_notify_plan(platform="windows", every_minutes=45)
    assert "/SC MINUTE /MO 45" in win.install
    assert "JarvisNotify" in win.install


def test_notify_plan_rejects_a_zero_interval():
    from jarvis.scheduling import build_notify_plan
    raised = False
    try:
        build_notify_plan(platform="unix", every_minutes=0)
    except ValueError:
        raised = True
    assert raised


# --- fixes confirmed by adversarial review -----------------------------------

def test_windows_tr_escapes_inner_quotes_for_paths_with_spaces():
    """schtasks rejects /TR when inner quotes aren't escaped, which a path
    containing a space always produces."""
    from jarvis.scheduling import build_notify_plan
    plan = build_notify_plan(
        platform="windows", python="C:/Program Files/py.exe",
        project_root="C:/My Projects", every_minutes=30,
    )
    assert chr(92) + chr(34) in plan.install


def test_windows_rejects_intervals_schtasks_cannot_express():
    from jarvis.scheduling import build_notify_plan
    raised = False
    try:
        build_notify_plan(platform="windows", every_minutes=2000)   # > 1439
    except ValueError:
        raised = True
    assert raised


def test_quoting_follows_the_requested_platform_not_this_machine():
    """A plan composed for another OS must carry that OS's quoting."""
    from jarvis.scheduling import jarvis_command
    assert jarvis_command("notify", python="/p/py", project_root="/r",
                          target="windows").startswith("cd /d")
    assert jarvis_command("notify", python="/p/py", project_root="/r",
                          target="unix").startswith("cd /r")


def test_cron_is_honest_about_uneven_and_inexpressible_cadences():
    from jarvis.scheduling import build_notify_plan
    uneven = build_notify_plan(platform="unix", every_minutes=7)
    assert "uneven" in uneven.note          # */7 restarts each hour
    inexpressible = build_notify_plan(platform="unix", every_minutes=90)
    assert "can't express" in inexpressible.note
    assert "0 */2" in inexpressible.install  # rounded, and said so
    even = build_notify_plan(platform="unix", every_minutes=30)
    assert "uneven" not in even.note


def test_launchd_plist_survives_special_characters_in_paths():
    """An & or < in a path would otherwise produce a plist launchd can't parse."""
    import plistlib
    from jarvis.scheduling import build_notify_plan
    plan = build_notify_plan(
        platform="macos", python="/opt/Tom & Jerry/py3",
        project_root="/tmp/a<b>", every_minutes=45,
    )
    after = plan.install.split("<<'PLIST'", 1)[1]
    xml = after.rsplit(chr(10) + "PLIST" + chr(10), 1)[0]
    doc = plistlib.loads(xml.strip().encode())
    assert doc["StartInterval"] == 2700
    assert doc["ProgramArguments"][0] == "/opt/Tom & Jerry/py3"
    assert doc["WorkingDirectory"] == "/tmp/a<b>"
