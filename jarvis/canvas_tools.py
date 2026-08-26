"""In-process SDK tools that give agents live, read-only Canvas access.

Until this module existed, ``jarvis/canvas.py`` could talk to Canvas but no
*agent* could: the client was reachable only through ``jarvis canvas sync``,
a command the user had to run by hand. The homework specialist was therefore
telling the truth when it said it had no Canvas access — it held file tools and
store tools and nothing else.

These tools close that gap. They follow the same factory pattern as
:mod:`jarvis.tools`: bound to a config at build time, no module-level globals,
trivially testable against a fake opener.

**Read-only, deliberately.** A Canvas access token also permits writes —
submitting assignments, posting to discussions, messaging classmates. None of
that is exposed here and none of it should be. An agent that reads your
coursework is a research assistant; one that can submit on your behalf turns a
misread deadline into a real, irreversible submission under your name.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

from .canvas import CanvasClient, CanvasConfig, CanvasError
from .courses import CourseProfiles

#: Name of the in-process MCP server; tools are ``mcp__jarvis_canvas__<tool>``.
SERVER_NAME = "jarvis_canvas"

#: Every tool this server exposes. Canvas access is read-only; the two profile
#: tools write, but only to Jarvis's own notes — never back to Canvas.
CANVAS_TOOL_NAMES = (
    "list_courses",
    "read_syllabus",
    "list_course_work",
    "list_course_files",
    "fetch_course_file",
    "list_announcements",
    "course_outline",
    "read_course_profile",
    "update_course_profile",
)

#: Cap on how much text one tool result may return. A syllabus or a long page
#: can be enormous, and an agent that swallows 200KB of HTML has spent its
#: context on boilerplate instead of reasoning.
MAX_CHARS = 12_000


def qualified(name: str) -> str:
    return f"mcp__{SERVER_NAME}__{name}"


def canvas_tool_names() -> list[str]:
    """Fully-qualified Canvas tools, for an agent's allowlist."""
    return [qualified(name) for name in CANVAS_TOOL_NAMES]


# --- helpers -----------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Flatten Canvas's HTML bodies into readable plain text.

    Syllabi and announcements come back as HTML. Feeding raw markup to a model
    wastes context on tags; this keeps the words, the links, and the block
    structure, and drops the rest.
    """

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "div", "br", "tr", "h1", "h2", "h3", "h4", "li"):
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and href.startswith("http"):
                self.parts.append(f" <{href}> ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Best-effort HTML -> text. Never raises on malformed markup."""
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:                      # malformed markup must not kill a tool
        return re.sub(r"<[^>]+>", " ", html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _clip(text: str) -> str:
    """Truncate at MAX_CHARS, saying so, so the agent knows it saw a slice."""
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f"\n\n[...truncated at {MAX_CHARS} characters]"


def _text(message: str, *, is_error: bool = False) -> dict:
    result: dict = {"content": [{"type": "text", "text": message}]}
    if is_error:
        result["is_error"] = True
    return result


#: Office formats we can unpack ourselves, mapped to a human label.
OFFICE_SUFFIXES = {".docx": "Word document", ".pptx": "PowerPoint deck"}

#: Text nodes live under different namespaces in Word and PowerPoint.
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DRAWING_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def docx_to_text(data: bytes) -> str:
    """Extract readable text from a .docx, using only the stdlib.

    Instructors hand out assignment directions as Word documents constantly,
    and the Read tool sees a .docx as binary noise. A .docx is a zip whose
    ``word/document.xml`` holds the prose, so the text is recoverable without
    taking on a dependency. Returns '' when the file isn't a usable .docx.
    """
    import io
    import zipfile
    from xml.etree import ElementTree

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            xml = bundle.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError):
        return ""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return ""

    lines: list[str] = []
    for paragraph in root.iter(f"{_WORD_NS}p"):
        pieces = [node.text or "" for node in paragraph.iter(f"{_WORD_NS}t")]
        line = "".join(pieces).strip()
        if line:
            lines.append(line)
    return "\n\n".join(lines)


def pptx_to_text(data: bytes) -> str:
    """Extract slide text from a .pptx, using only the stdlib.

    Lecture slides are where a lot of course content actually lives, and a
    .pptx is binary to the Read tool for the same reason a .docx is. Slides
    are ``ppt/slides/slideN.xml`` inside the zip; their text sits in DrawingML
    ``<a:t>`` nodes. Slides are numbered so the agent can cite one.
    """
    import io
    import re as _re
    import zipfile
    from xml.etree import ElementTree

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            names = [
                n for n in bundle.namelist()
                if _re.fullmatch(r"ppt/slides/slide\d+\.xml", n)
            ]
            names.sort(key=lambda n: int(_re.findall(r"\d+", n)[-1]))
            slides = [(n, bundle.read(n)) for n in names]
    except (zipfile.BadZipFile, KeyError, OSError, ValueError):
        return ""

    chunks: list[str] = []
    for index, (_name, xml) in enumerate(slides, start=1):
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            continue
        pieces = [node.text or "" for node in root.iter(f"{_DRAWING_NS}t")]
        body = "\n".join(p.strip() for p in pieces if p.strip())
        if body:
            chunks.append(f"--- Slide {index} ---\n{body}")
    return "\n\n".join(chunks)


def office_to_text(suffix: str, data: bytes) -> str:
    """Text from whichever Office format we recognize; '' if we don't."""
    if suffix == ".docx":
        return docx_to_text(data)
    if suffix == ".pptx":
        return pptx_to_text(data)
    return ""


def _safe_filename(name: str) -> str:
    """A Canvas filename reduced to something safe to write locally.

    Canvas filenames are attacker-adjacent input: they come from whoever
    uploaded the file. Stripping directory separators and leading dots keeps a
    download inside the folder we chose for it.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip("._ -")
    return cleaned[:120] or "canvas-file"


# --- tool factory ------------------------------------------------------------

def build_canvas_tools(
    config: Optional[CanvasConfig] = None,
    *,
    client: Optional[CanvasClient] = None,
    download_dir: Optional[Path] = None,
    profiles_dir: Optional[Path] = None,
) -> list[SdkMcpTool]:
    """Create the Canvas tools. ``client`` is injectable for tests."""

    _client: Optional[CanvasClient] = client
    _config = config
    profiles = CourseProfiles(profiles_dir)

    def canvas() -> CanvasClient:
        """Resolve the client lazily, so a missing token is a tool-level error
        the agent can report — not an import-time crash of the whole run."""
        nonlocal _client
        if _client is None:
            _client = CanvasClient(_config or CanvasConfig.from_env())
        return _client

    def resolve_course(name: str) -> tuple[Optional[dict], str]:
        """Find one course by fuzzy name. Returns (course, error_message)."""
        try:
            courses = canvas().active_courses()
        except CanvasError as exc:
            return None, str(exc)
        needle = (name or "").strip().lower()
        if not needle:
            return None, "No course name given."
        named = [c for c in courses if isinstance(c, dict) and c.get("name")]
        exact = [c for c in named if c["name"].lower() == needle]
        if exact:
            return exact[0], ""
        partial = [
            c for c in named
            if needle in c["name"].lower()
            or needle in str(c.get("course_code", "")).lower()
        ]
        if len(partial) == 1:
            return partial[0], ""
        if len(partial) > 1:
            options = ", ".join(c["name"] for c in partial)
            return None, f"{name!r} matches several courses: {options}. Be specific."
        available = ", ".join(c["name"] for c in named)
        return None, f"No course matching {name!r}. You are enrolled in: {available}"

    course_arg = {
        "course": {
            "type": "string",
            "description": "Course name or code, e.g. 'AAM-1730' or 'CSCI-1050'.",
        }
    }

    @tool(
        "list_courses",
        "List the user's active Canvas courses. Call this first when you need to "
        "know what classes they are actually taking.",
        {"type": "object", "properties": {}},
    )
    async def list_courses(args: dict) -> dict:
        try:
            courses = canvas().active_courses()
        except CanvasError as exc:
            return _text(str(exc), is_error=True)
        named = [c for c in courses if isinstance(c, dict) and c.get("name")]
        if not named:
            return _text("No active courses found in Canvas.")
        lines = [f"{len(named)} active course(s):"]
        for course in sorted(named, key=lambda c: str(c["name"])):
            lines.append(f"  - {course['name']}")
        return _text("\n".join(lines))

    @tool(
        "read_syllabus",
        "Read a course's syllabus. The single best source for grading weights, "
        "late policy, expected workload, and how the course is structured.",
        {"type": "object", "properties": course_arg, "required": ["course"]},
    )
    async def read_syllabus(args: dict) -> dict:
        course, error = resolve_course(args.get("course", ""))
        if error:
            return _text(error, is_error=True)
        assert course is not None
        try:
            body = canvas().course_syllabus(int(course["id"]))
        except CanvasError as exc:
            return _text(str(exc), is_error=True)
        text = html_to_text(body)
        if not text:
            return _text(
                f"{course['name']} has no syllabus posted on Canvas. "
                "Try the course files or pages instead."
            )
        return _text(f"Syllabus — {course['name']}\n\n{_clip(text)}")

    @tool(
        "list_course_work",
        "List a course's assignments with due dates, points, and whether they "
        "have been submitted.",
        {"type": "object", "properties": course_arg, "required": ["course"]},
    )
    async def list_course_work(args: dict) -> dict:
        course, error = resolve_course(args.get("course", ""))
        if error:
            return _text(error, is_error=True)
        assert course is not None
        try:
            assignments = canvas().course_assignments(int(course["id"]))
        except CanvasError as exc:
            return _text(str(exc), is_error=True)
        if not assignments:
            return _text(f"{course['name']} has no assignments posted.")
        lines = [f"{course['name']} — {len(assignments)} assignment(s):"]
        for item in assignments:
            if not isinstance(item, dict):
                continue
            due = item.get("due_at") or "no due date"
            points = item.get("points_possible")
            points_text = f", {points} pts" if points is not None else ""
            lines.append(f"  - {item.get('name', 'untitled')} (due {due}{points_text})")
        return _text(_clip("\n".join(lines)))

    @tool(
        "list_course_files",
        "List files posted in a course — slides, readings, handouts. Use this to "
        "find what to read, then fetch_course_file to actually read one.",
        {"type": "object", "properties": course_arg, "required": ["course"]},
    )
    async def list_course_files(args: dict) -> dict:
        course, error = resolve_course(args.get("course", ""))
        if error:
            return _text(error, is_error=True)
        assert course is not None
        try:
            files = canvas().course_files(int(course["id"]))
        except CanvasError as exc:
            return _text(str(exc), is_error=True)
        if not files:
            return _text(
                f"{course['name']} has no files available "
                "(the instructor may have the Files tab turned off)."
            )
        lines = [f"{course['name']} — {len(files)} file(s):"]
        for item in files:
            if not isinstance(item, dict):
                continue
            name = item.get("display_name") or item.get("filename") or "untitled"
            size = item.get("size")
            size_text = f" ({round(size / 1024)} KB)" if isinstance(size, int) else ""
            lines.append(f"  - {name}{size_text}")
        return _text(_clip("\n".join(lines)))

    @tool(
        "fetch_course_file",
        "Download one course file into scratch/canvas/ and return its local path. "
        "Then use the Read tool on that path to actually read it (Read handles "
        "PDFs). Match the file by name as shown in list_course_files.",
        {
            "type": "object",
            "properties": {
                **course_arg,
                "filename": {
                    "type": "string",
                    "description": "The file's name, or a distinctive part of it.",
                },
            },
            "required": ["course", "filename"],
        },
    )
    async def fetch_course_file(args: dict) -> dict:
        course, error = resolve_course(args.get("course", ""))
        if error:
            return _text(error, is_error=True)
        assert course is not None
        needle = str(args.get("filename", "")).strip().lower()
        if not needle:
            return _text("No filename given.", is_error=True)
        try:
            files = canvas().course_files(int(course["id"]))
        except CanvasError as exc:
            return _text(str(exc), is_error=True)

        def label(item: dict) -> str:
            return str(item.get("display_name") or item.get("filename") or "")

        matches = [
            f for f in files
            if isinstance(f, dict) and needle in label(f).lower()
        ]
        if not matches:
            available = ", ".join(label(f) for f in files if isinstance(f, dict))
            return _text(
                f"No file matching {needle!r} in {course['name']}. "
                f"Available: {available or 'none'}",
                is_error=True,
            )
        if len(matches) > 1:
            options = ", ".join(label(f) for f in matches)
            return _text(
                f"{needle!r} matches several files: {options}. Be more specific.",
                is_error=True,
            )

        chosen = matches[0]
        url = chosen.get("url")
        if not url:
            return _text(
                f"{label(chosen)!r} has no downloadable URL "
                "(it may be a link rather than a file).",
                is_error=True,
            )
        target_dir = download_dir or (Path.cwd() / "scratch" / "canvas")
        dest = target_dir / _safe_filename(label(chosen))
        try:
            saved = canvas().download_file(str(url), dest)
        except (CanvasError, OSError) as exc:
            return _text(f"Could not download {label(chosen)!r}: {exc}", is_error=True)

        # Word and PowerPoint are how assignment directions and lecture slides
        # actually arrive, and Read sees both as binary. Convert here so the
        # agent gets prose instead of a file it must report as unreadable.
        suffix = saved.suffix.lower()
        if suffix in OFFICE_SUFFIXES:
            try:
                extracted = office_to_text(suffix, saved.read_bytes())
            except OSError:
                extracted = ""
            if extracted:
                companion = saved.with_suffix(suffix + ".txt")
                try:
                    companion.write_text(extracted, encoding="utf-8")
                except OSError:
                    pass
                else:
                    return _text(
                        f"Saved {label(chosen)!r} to {saved} and extracted its "
                        f"text to {companion}\n\n{_clip(extracted)}"
                    )
            return _text(
                f"Saved {label(chosen)!r} to {saved}, but no text could be "
                f"extracted from this {OFFICE_SUFFIXES[suffix]} — it may be "
                "image-only or an unusual format. Tell the user rather than "
                "guessing at the contents.",
                is_error=True,
            )

        return _text(
            f"Saved {label(chosen)!r} to {saved}\n"
            f"Use the Read tool on that path to read it."
        )

    @tool(
        "list_announcements",
        "Recent course announcements — where instructors post schedule changes, "
        "deadline extensions, and exam details.",
        {"type": "object", "properties": course_arg, "required": ["course"]},
    )
    async def list_announcements(args: dict) -> dict:
        course, error = resolve_course(args.get("course", ""))
        if error:
            return _text(error, is_error=True)
        assert course is not None
        try:
            posts = canvas().course_announcements(int(course["id"]))
        except CanvasError as exc:
            return _text(str(exc), is_error=True)
        if not posts:
            return _text(f"{course['name']} has no recent announcements.")
        chunks = [f"{course['name']} — {len(posts)} announcement(s):"]
        for post in posts:
            if not isinstance(post, dict):
                continue
            posted = str(post.get("posted_at") or "")[:10]
            body = html_to_text(post.get("message") or "")
            chunks.append(
                f"\n[{posted}] {post.get('title', 'untitled')}\n{body[:1500]}"
            )
        return _text(_clip("\n".join(chunks)))

    @tool(
        "course_outline",
        "The course's modules and their items, in the instructor's own order — "
        "the best view of how a course is sequenced week to week.",
        {"type": "object", "properties": course_arg, "required": ["course"]},
    )
    async def course_outline(args: dict) -> dict:
        course, error = resolve_course(args.get("course", ""))
        if error:
            return _text(error, is_error=True)
        assert course is not None
        try:
            modules = canvas().course_modules(int(course["id"]))
        except CanvasError as exc:
            return _text(str(exc), is_error=True)
        if not modules:
            return _text(
                f"{course['name']} has no modules "
                "(the instructor may organize the course another way)."
            )
        lines = [f"{course['name']} — course outline:"]
        for module in modules:
            if not isinstance(module, dict):
                continue
            lines.append(f"\n{module.get('name', 'untitled module')}")
            for entry in module.get("items") or []:
                if isinstance(entry, dict):
                    lines.append(
                        f"  - [{entry.get('type', '?')}] {entry.get('title', 'untitled')}"
                    )
        return _text(_clip("\n".join(lines)))

    @tool(
        "read_course_profile",
        "Read what Jarvis already knows about a course — grading weights, "
        "workload, what the instructor expects, recurring assignment patterns. "
        "ALWAYS call this before doing coursework for a class, so you build on "
        "what was already learned instead of starting cold.",
        {"type": "object", "properties": course_arg, "required": ["course"]},
    )
    async def read_course_profile(args: dict) -> dict:
        name = str(args.get("course", "")).strip()
        if not name:
            return _text("No course name given.", is_error=True)
        profile = profiles.load(name)
        if not profile.exists():
            return _text(
                f"No profile for {name!r} yet — this is a cold start.\n"
                "Build one: read the syllabus, skim the outline, then call "
                "update_course_profile. Starting template:\n\n" + profile.body
            )
        return _text(f"Profile — {name} (from {profile.path})\n\n{_clip(profile.body)}")

    @tool(
        "update_course_profile",
        "Save what you learned about a course so future runs start warm. Pass "
        "the COMPLETE updated Markdown document, not a fragment — it replaces "
        "the file. Read the existing profile first and preserve what is still "
        "true, especially anything the user corrected by hand.",
        {
            "type": "object",
            "properties": {
                **course_arg,
                "body": {
                    "type": "string",
                    "description": "The full Markdown profile, replacing the old one.",
                },
            },
            "required": ["course", "body"],
        },
    )
    async def update_course_profile(args: dict) -> dict:
        name = str(args.get("course", "")).strip()
        body = str(args.get("body", ""))
        if not name:
            return _text("No course name given.", is_error=True)
        if not body.strip():
            return _text(
                "Refusing to save an empty profile — that would erase what is "
                "already known about this course.",
                is_error=True,
            )
        try:
            saved = profiles.save(name, body)
        except OSError as exc:
            return _text(f"Could not save the profile: {exc}", is_error=True)
        return _text(f"Saved the {name} profile to {saved}.")

    return [
        list_courses,
        read_syllabus,
        list_course_work,
        list_course_files,
        fetch_course_file,
        list_announcements,
        course_outline,
        read_course_profile,
        update_course_profile,
    ]


def build_canvas_server(
    config: Optional[CanvasConfig] = None,
    *,
    client: Optional[CanvasClient] = None,
    download_dir: Optional[Path] = None,
    profiles_dir: Optional[Path] = None,
):
    """The in-process MCP server carrying the Canvas tools."""
    return create_sdk_mcp_server(
        name=SERVER_NAME,
        tools=build_canvas_tools(
            config, client=client,
            download_dir=download_dir, profiles_dir=profiles_dir,
        ),
    )
