"""Tests for user-uploaded reference files. Offline, temp directories."""

import base64
import tempfile
from pathlib import Path

from jarvis.uploads import (
    MAX_UPLOAD_BYTES,
    UploadError,
    UploadStore,
    describe_for_prompt,
    safe_filename,
)

from tests.test_canvas_tools import _make_docx


def _store() -> UploadStore:
    return UploadStore(Path(tempfile.mkdtemp(prefix="jarvis-up-")))


# --- storing -----------------------------------------------------------------

def test_a_file_round_trips():
    store = _store()
    saved = store.save("rubric.txt", b"Thesis: 30 points")
    assert saved.path.read_bytes() == b"Thesis: 30 points"
    assert saved.size == 17
    assert [u.name for u in store.all()] == ["rubric.txt"]


def test_an_empty_file_is_refused():
    raised = False
    try:
        _store().save("empty.txt", b"")
    except UploadError:
        raised = True
    assert raised


def test_an_oversized_file_is_refused_with_its_size():
    store = _store()
    try:
        store.save("huge.pdf", b"x" * (MAX_UPLOAD_BYTES + 1))
        assert False, "expected a refusal"
    except UploadError as exc:
        assert "limit is" in str(exc)
    assert store.all() == []


def test_a_hostile_filename_cannot_escape_the_upload_directory():
    """The name comes from the user's filesystem, so it is not ours to trust."""
    store = _store()
    saved = store.save("../../../../etc/passwd", b"data")
    assert saved.path.parent == store.directory
    assert "/" not in saved.name


def test_safe_filename_strips_separators_and_dots():
    assert "/" not in safe_filename("../../etc/passwd")
    assert not safe_filename("...hidden").startswith(".")
    assert safe_filename("///") == "upload"


def test_re_uploading_replaces_rather_than_duplicating():
    store = _store()
    store.save("paper.txt", b"draft one")
    store.save("paper.txt", b"draft two")
    assert len(store.all()) == 1
    assert store.resolve("paper.txt").path.read_bytes() == b"draft two"


# --- Office extraction -------------------------------------------------------

def test_a_word_upload_gets_a_readable_text_companion():
    """A rubric almost always arrives as .docx, which Read sees as binary."""
    store = _store()
    saved = store.save("rubric.docx", _make_docx(["Thesis 30", "Evidence 40"]))
    assert saved.text_path is not None
    text = saved.text_path.read_text()
    assert "Thesis 30" in text and "Evidence 40" in text


def test_the_companion_is_not_listed_as_a_separate_upload():
    store = _store()
    store.save("rubric.docx", _make_docx(["x"]))
    assert [u.name for u in store.all()] == ["rubric.docx"]


def test_a_corrupt_word_file_still_uploads():
    """The bytes are the user's; failing to parse them must not lose them."""
    store = _store()
    saved = store.save("broken.docx", b"not really a docx")
    assert saved.path.exists() and saved.text_path is None


# --- removal -----------------------------------------------------------------

def test_removing_an_upload_takes_its_companion_too():
    store = _store()
    saved = store.save("notes.docx", _make_docx(["hello"]))
    companion = saved.text_path
    assert store.remove("notes.docx") is True
    assert not saved.path.exists() and not companion.exists()


def test_removing_something_absent_reports_false():
    assert _store().remove("nope.txt") is False


def test_resolve_rejects_a_path_rather_than_a_name():
    store = _store()
    store.save("paper.txt", b"x")
    assert store.resolve("../paper.txt") is None
    assert store.resolve("paper.txt") is not None


# --- telling the agent -------------------------------------------------------

def test_the_prompt_note_points_at_the_readable_file():
    """A .docx path would be reported unreadable; the companion is the one
    the agent can actually open."""
    store = _store()
    saved = store.save("rubric.docx", _make_docx(["Thesis 30"]))
    note = describe_for_prompt([saved])
    assert "rubric.docx" in note
    assert str(saved.text_path) in note
    assert "Read them before answering" in note


def test_a_plain_file_is_referenced_directly():
    store = _store()
    saved = store.save("paper.txt", b"essay text")
    assert str(saved.path) in describe_for_prompt([saved])


def test_no_attachments_adds_nothing_to_the_prompt():
    assert describe_for_prompt([]) == ""
