"""Tests for the live Canvas tools. Fully offline — the client is injected."""

import asyncio
import tempfile
from pathlib import Path

from jarvis.canvas import CanvasError
from jarvis.canvas_tools import (
    MAX_CHARS,
    _safe_filename,
    build_canvas_tools,
    canvas_tool_names,
    html_to_text,
)

COURSES = [
    {"id": 1, "name": "AAM-1730-01", "course_code": "AAM1730"},
    {"id": 2, "name": "CSCI-1050-01", "course_code": "CSCI1050"},
    {"id": 3, "name": "CSCI-2510-01", "course_code": "CSCI2510"},
]


class FakeCanvas:
    """Stands in for CanvasClient with canned, controllable responses."""

    def __init__(self, **overrides):
        self.calls: list[str] = []
        self.overrides = overrides
        self.downloaded: list[str] = []

    def _answer(self, name, default):
        self.calls.append(name)
        value = self.overrides.get(name, default)
        if isinstance(value, Exception):
            raise value
        return value

    def active_courses(self):
        return self._answer("active_courses", COURSES)

    def course_assignments(self, course_id):
        return self._answer("course_assignments", [
            {"name": "Essay 1", "due_at": "2026-09-01T04:59:00Z", "points_possible": 100},
        ])

    def course_files(self, course_id):
        return self._answer("course_files", [
            {"display_name": "Syllabus.pdf", "url": "https://x/1", "size": 2048},
            {"display_name": "Week 1 Reading.pdf", "url": "https://x/2", "size": 4096},
        ])

    def course_announcements(self, course_id):
        return self._answer("course_announcements", [
            {"title": "Exam moved", "posted_at": "2026-08-20T12:00:00Z",
             "message": "<p>The exam is <b>next Friday</b>.</p>"},
        ])

    def course_modules(self, course_id):
        return self._answer("course_modules", [
            {"name": "Week 1", "items": [{"type": "Page", "title": "Intro"}]},
        ])

    def course_syllabus(self, course_id):
        return self._answer("course_syllabus", "<p>Grade: 40% essays</p>")

    def download_file(self, url, dest):
        self.downloaded.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"pdf bytes")
        return dest


def _tools(fake=None, **kwargs):
    """Build the tools against a fake, keyed by bare tool name."""
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-ctools-"))
    built = build_canvas_tools(
        client=fake or FakeCanvas(),
        download_dir=kwargs.pop("download_dir", tmp / "dl"),
        profiles_dir=kwargs.pop("profiles_dir", tmp / "profiles"),
    )
    return {t.name: t for t in built}


def _run(tool, args):
    return asyncio.run(tool.handler(args))


def _body(result):
    return result["content"][0]["text"]


# --- wiring ------------------------------------------------------------------

def test_every_declared_tool_is_actually_built():
    """The allowlist and the implementation must not drift apart — a name in
    one and not the other is a tool the agent is told it has but cannot call."""
    built = set(_tools())
    declared = {n.rsplit("__", 1)[-1] for n in canvas_tool_names()}
    assert built == declared


# --- course resolution -------------------------------------------------------

def test_course_resolves_by_partial_name_and_code():
    tools = _tools()
    assert "40% essays" in _body(_run(tools["read_syllabus"], {"course": "AAM-1730"}))
    assert "40% essays" in _body(_run(tools["read_syllabus"], {"course": "aam1730"}))


def test_ambiguous_course_asks_instead_of_guessing():
    """'CSCI' matches two courses. Picking one silently would send the user
    work for the wrong class."""
    result = _run(_tools()["read_syllabus"], {"course": "CSCI"})
    assert result.get("is_error")
    assert "matches several" in _body(result)
    assert "CSCI-1050-01" in _body(result) and "CSCI-2510-01" in _body(result)


def test_unknown_course_lists_what_is_available():
    result = _run(_tools()["read_syllabus"], {"course": "PHYS-9999"})
    assert result.get("is_error")
    assert "AAM-1730-01" in _body(result)


# --- reading -----------------------------------------------------------------

def test_syllabus_html_becomes_plain_text():
    body = _body(_run(_tools()["read_syllabus"], {"course": "AAM"}))
    assert "40% essays" in body and "<p>" not in body


def test_a_course_without_a_syllabus_says_so_rather_than_inventing_one():
    tools = _tools(FakeCanvas(course_syllabus=""))
    body = _body(_run(tools["read_syllabus"], {"course": "AAM"}))
    assert "no syllabus posted" in body


def test_disabled_tabs_report_emptiness_not_failure():
    tools = _tools(FakeCanvas(course_files=[], course_modules=[]))
    files = _body(_run(tools["list_course_files"], {"course": "AAM"}))
    outline = _body(_run(tools["course_outline"], {"course": "AAM"}))
    assert "no files available" in files
    assert "no modules" in outline


def test_announcement_html_is_flattened():
    body = _body(_run(_tools()["list_announcements"], {"course": "AAM"}))
    assert "Exam moved" in body and "next Friday" in body and "<b>" not in body


# --- downloading -------------------------------------------------------------

def test_fetch_file_saves_and_returns_a_readable_path():
    fake = FakeCanvas()
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-dl-"))
    tools = _tools(fake, download_dir=tmp)
    body = _body(_run(tools["fetch_course_file"], {"course": "AAM", "filename": "Syllabus"}))
    saved = tmp / "Syllabus.pdf"
    assert saved.exists() and str(saved) in body
    assert "Read tool" in body


def test_ambiguous_filename_refuses_rather_than_guessing():
    result = _run(_tools()["fetch_course_file"], {"course": "AAM", "filename": ".pdf"})
    assert result.get("is_error") and "matches several files" in _body(result)


def test_a_hostile_filename_cannot_escape_the_download_directory():
    """The filename comes from whoever uploaded the file, so it is untrusted."""
    fake = FakeCanvas(course_files=[
        {"display_name": "../../../../etc/passwd", "url": "https://x/evil"},
    ])
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-esc-"))
    tools = _tools(fake, download_dir=tmp)
    _run(tools["fetch_course_file"], {"course": "AAM", "filename": "passwd"})
    written = list(tmp.rglob("*"))
    assert written, "expected the download to land somewhere"
    for path in written:
        assert tmp.resolve() in path.resolve().parents or path.parent == tmp


def test_safe_filename_strips_separators_and_leading_dots():
    assert "/" not in _safe_filename("../../etc/passwd")
    assert not _safe_filename("...hidden").startswith(".")
    assert _safe_filename("///") == "canvas-file"


# --- failure handling --------------------------------------------------------

def test_a_canvas_error_is_reported_not_raised():
    """A 401 must reach the agent as a message it can relay, not an exception
    that kills the whole run."""
    tools = _tools(FakeCanvas(active_courses=CanvasError("Canvas rejected the token (401).")))
    result = _run(tools["list_courses"], {})
    assert result.get("is_error") and "401" in _body(result)


def test_oversized_text_is_clipped_and_says_so():
    tools = _tools(FakeCanvas(course_syllabus="<p>" + ("x" * (MAX_CHARS + 5000)) + "</p>"))
    body = _body(_run(tools["read_syllabus"], {"course": "AAM"}))
    assert "truncated" in body and len(body) < MAX_CHARS + 500


# --- html helper -------------------------------------------------------------

def test_html_to_text_survives_malformed_markup():
    assert "hello" in html_to_text("<p>hello<<<>")


def test_html_to_text_drops_script_contents():
    assert "alert" not in html_to_text("<script>alert(1)</script><p>real</p>")


# --- Word documents ----------------------------------------------------------

def _make_docx(paragraphs) -> bytes:
    """Build a minimal but real .docx in memory."""
    import io
    import zipfile

    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_docx_text_is_extracted():
    from jarvis.canvas_tools import docx_to_text
    text = docx_to_text(_make_docx(["Final Project", "Make a horror short."]))
    assert "Final Project" in text and "horror short" in text


def test_a_non_docx_returns_empty_rather_than_raising():
    from jarvis.canvas_tools import docx_to_text
    assert docx_to_text(b"not a zip at all") == ""
    assert docx_to_text(b"") == ""


def test_fetching_a_docx_returns_its_prose_inline():
    """Assignment directions arrive as Word files; the agent must get the text,
    not a path it will report as unreadable."""
    payload = _make_docx(["Directions", "Two pages, double spaced."])

    class DocxCanvas(FakeCanvas):
        def course_files(self, course_id):
            return [{"display_name": "Project.docx", "url": "https://x/d"}]

        def download_file(self, url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(payload)
            return dest

    tmp = Path(tempfile.mkdtemp(prefix="jarvis-docx-"))
    tools = _tools(DocxCanvas(), download_dir=tmp)
    body = _body(_run(tools["fetch_course_file"], {"course": "AAM", "filename": "Project"}))
    assert "Two pages, double spaced." in body
    assert (tmp / "Project.docx.txt").exists()


def test_an_unparseable_docx_is_reported_not_silently_empty():
    class BadDocx(FakeCanvas):
        def course_files(self, course_id):
            return [{"display_name": "Broken.docx", "url": "https://x/b"}]

        def download_file(self, url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"corrupt")
            return dest

    tmp = Path(tempfile.mkdtemp(prefix="jarvis-docx-"))
    tools = _tools(BadDocx(), download_dir=tmp)
    result = _run(tools["fetch_course_file"], {"course": "AAM", "filename": "Broken"})
    assert result.get("is_error") and "Word document" in _body(result)


def _make_pptx(slides) -> bytes:
    """Build a minimal but real .pptx in memory."""
    import io
    import zipfile

    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for number, lines in enumerate(slides, start=1):
            body = "".join(f"<a:t>{line}</a:t>" for line in lines)
            bundle.writestr(
                f"ppt/slides/slide{number}.xml",
                f'<?xml version="1.0"?><p:sld xmlns:a="{ns}" '
                f'xmlns:p="http://x"><p:cSld>{body}</p:cSld></p:sld>',
            )
    return buffer.getvalue()


def test_pptx_slide_text_is_extracted_with_slide_numbers():
    from jarvis.canvas_tools import pptx_to_text
    text = pptx_to_text(_make_pptx([["Chapter 1"], ["Learning Objectives"]]))
    assert "Slide 1" in text and "Chapter 1" in text
    assert "Slide 2" in text and "Learning Objectives" in text


def test_pptx_slides_stay_in_numeric_order():
    """Zip entries are not ordered, and slide10 sorts before slide2 as text —
    an out-of-order deck would make the agent cite the wrong slide."""
    from jarvis.canvas_tools import pptx_to_text
    text = pptx_to_text(_make_pptx([[f"content{i}"] for i in range(1, 12)]))
    assert text.index("Slide 2 ") < text.index("Slide 10 ")


def test_a_non_pptx_returns_empty_rather_than_raising():
    from jarvis.canvas_tools import pptx_to_text
    assert pptx_to_text(b"not a zip") == ""


def test_fetching_a_pptx_returns_its_slide_text():
    payload = _make_pptx([["Intro to Financial Statements"]])

    class DeckCanvas(FakeCanvas):
        def course_files(self, course_id):
            return [{"display_name": "Ch01 Slides.pptx", "url": "https://x/p"}]

        def download_file(self, url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(payload)
            return dest

    tmp = Path(tempfile.mkdtemp(prefix="jarvis-pptx-"))
    tools = _tools(DeckCanvas(), download_dir=tmp)
    body = _body(_run(tools["fetch_course_file"], {"course": "AAM", "filename": "Ch01"}))
    assert "Intro to Financial Statements" in body
    assert (tmp / "Ch01 Slides.pptx.txt").exists()


def test_an_image_only_deck_is_reported_not_silently_empty():
    class EmptyDeck(FakeCanvas):
        def course_files(self, course_id):
            return [{"display_name": "Scans.pptx", "url": "https://x/s"}]

        def download_file(self, url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(_make_pptx([[]]))
            return dest

    tmp = Path(tempfile.mkdtemp(prefix="jarvis-pptx-"))
    tools = _tools(EmptyDeck(), download_dir=tmp)
    result = _run(tools["fetch_course_file"], {"course": "AAM", "filename": "Scans"})
    assert result.get("is_error") and "PowerPoint deck" in _body(result)
