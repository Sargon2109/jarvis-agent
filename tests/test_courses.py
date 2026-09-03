"""Tests for course profiles — the agent's durable per-course memory."""

import asyncio
import tempfile
from pathlib import Path

from jarvis.canvas_tools import build_canvas_tools
from jarvis.courses import CourseProfiles, course_slug

from tests.test_canvas_tools import FakeCanvas


def _profiles() -> CourseProfiles:
    return CourseProfiles(Path(tempfile.mkdtemp(prefix="jarvis-courses-")))


# --- the store ---------------------------------------------------------------

def test_a_missing_profile_loads_the_template_rather_than_failing():
    profile = _profiles().load("AAM-1730-01")
    assert not profile.exists()
    assert "AAM-1730-01" in profile.body
    assert "How the grade is built" in profile.body


def test_saving_then_loading_round_trips():
    profiles = _profiles()
    profiles.save("CSCI-1050-01", "# CSCI\n\n40% labs.")
    loaded = profiles.load("CSCI-1050-01")
    assert loaded.exists() and "40% labs" in loaded.body


def test_profiles_are_listed_once_written():
    profiles = _profiles()
    assert profiles.known() == []
    profiles.save("AAM-1730-01", "x")
    assert profiles.known() == ["aam-1730-01"]


def test_slug_keeps_a_course_name_inside_the_directory():
    """Course names come from Canvas, so they are not ours to trust."""
    assert "/" not in course_slug("../../etc/passwd")
    assert course_slug("AAM-1730-01") == "aam-1730-01"
    assert course_slug("") == "course"


def test_a_corrupt_profile_file_degrades_to_the_template():
    profiles = _profiles()
    path = profiles.path_for("AAM")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe invalid utf-8")
    assert "How the grade is built" in profiles.load("AAM").body


def test_saves_are_atomic_leaving_no_temp_files_behind():
    profiles = _profiles()
    profiles.save("AAM", "content")
    leftovers = [p.name for p in profiles.directory.iterdir() if p.name.startswith(".")]
    assert leftovers == []


# --- the tools ---------------------------------------------------------------

def _tools(profiles_dir):
    built = build_canvas_tools(
        client=FakeCanvas(),
        download_dir=profiles_dir / "dl",
        profiles_dir=profiles_dir,
    )
    return {t.name: t for t in built}


def _run(tool, args):
    return asyncio.run(tool.handler(args))


def _body(result):
    return result["content"][0]["text"]


def test_reading_an_unknown_profile_hands_back_a_starting_template():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-cp-"))
    body = _body(_run(_tools(tmp)["read_course_profile"], {"course": "AAM-1730"}))
    assert "cold start" in body and "How the grade is built" in body


def test_update_then_read_returns_what_was_learned():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-cp-"))
    tools = _tools(tmp)
    _run(tools["update_course_profile"],
         {"course": "AAM-1730", "body": "# AAM\n\nGrade: 40% essays, 60% final."})
    body = _body(_run(tools["read_course_profile"], {"course": "AAM-1730"}))
    assert "40% essays" in body


def test_an_empty_body_is_refused_so_knowledge_is_never_erased():
    """The tool replaces the whole document, so an empty write would silently
    destroy everything already learned about the course."""
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-cp-"))
    tools = _tools(tmp)
    _run(tools["update_course_profile"], {"course": "AAM", "body": "real knowledge"})
    result = _run(tools["update_course_profile"], {"course": "AAM", "body": "   "})
    assert result.get("is_error")
    assert "real knowledge" in _body(_run(tools["read_course_profile"], {"course": "AAM"}))


def test_profile_tools_do_not_require_canvas_to_be_reachable():
    """Profiles are Jarvis's own notes. They must stay readable when Canvas is
    down or unconfigured, since that is exactly when they are most useful."""
    from jarvis.canvas import CanvasError

    tmp = Path(tempfile.mkdtemp(prefix="jarvis-cp-"))
    built = build_canvas_tools(
        client=FakeCanvas(active_courses=CanvasError("no token")),
        profiles_dir=tmp,
    )
    tools = {t.name: t for t in built}
    _run(tools["update_course_profile"], {"course": "AAM", "body": "known offline"})
    assert "known offline" in _body(
        _run(tools["read_course_profile"], {"course": "AAM"})
    )


# --- profiles are keyed to Canvas's own course name --------------------------

def test_the_same_course_named_differently_shares_one_profile():
    """'AAM-1730' and 'AAM-1730-01' are the same class. Filing them separately
    splits the course's knowledge and makes the second run start cold."""
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-canon-"))
    tools = _tools(tmp)
    _run(tools["update_course_profile"],
         {"course": "AAM-1730", "body": "# AAM\n\nGrade: 40% essays."})
    body = _body(_run(tools["read_course_profile"], {"course": "AAM-1730-01"}))
    assert "40% essays" in body
    assert len(list(tmp.glob("*.md"))) == 1


def test_a_short_code_and_the_full_name_agree():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-canon-"))
    tools = _tools(tmp)
    _run(tools["update_course_profile"], {"course": "aam1730", "body": "learned"})
    assert "learned" in _body(_run(tools["read_course_profile"], {"course": "AAM-1730-01"}))
    assert [p.stem for p in tmp.glob("*.md")] == ["aam-1730-01"]


def test_an_unresolvable_course_still_gets_a_profile():
    """Canvas being unreachable must not make profiles unwritable."""
    from jarvis.canvas import CanvasError

    tmp = Path(tempfile.mkdtemp(prefix="jarvis-canon-"))
    built = build_canvas_tools(
        client=FakeCanvas(active_courses=CanvasError("offline")), profiles_dir=tmp
    )
    tools = {t.name: t for t in built}
    _run(tools["update_course_profile"], {"course": "PHYS-1000", "body": "offline note"})
    assert "offline note" in _body(
        _run(tools["read_course_profile"], {"course": "PHYS-1000"})
    )
