"""Course profiles — the part that makes the homework agent get better.

You cannot fine-tune the model behind a specialist, so "training the agent on
my courses" has to mean something else: **durable notes the agent writes once
and reloads every time it touches that course.** Grading weights, reading
cadence, what a given professor actually rewards, the shape of each recurring
assignment. Pulled from the syllabus the first time, corrected by the user over
the term, and read back before any real work.

That turns each course into accumulated context instead of a cold start. The
second week of AAM 1730 should not require rediscovering how AAM 1730 works.

One Markdown file per course under ``data/courses/``. Markdown, not JSON,
because the agent writes prose here and the user should be able to open the
file and correct it by hand — the same principle as the rest of Jarvis's data.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Fallback location: <repo>/data/courses/ (data/ is gitignored).
DEFAULT_COURSES_DIR = Path(__file__).resolve().parent.parent / "data" / "courses"

#: Env var to relocate course profiles, matching the other data paths.
COURSES_DIR_ENV = "JARVIS_COURSES_DIR"

#: The skeleton a brand-new profile starts from. The headings are prompts to
#: the agent about what is worth knowing, not a schema it must obey.
PROFILE_TEMPLATE = """# {name}

## What this course is
_One or two sentences: subject, level, how it is taught._

## How the grade is built
_Weights per category. Straight from the syllabus when possible._

## Workload and cadence
_Reading per week, when assignments drop, when they are due._

## What the instructor actually wants
_Expected form and depth of work. Style, citation, length, participation._

## Recurring assignment patterns
_The shapes that repeat, and what a strong version of each looks like._

## Notes and corrections
_Anything the user has told Jarvis directly about this course._
"""


def default_courses_dir() -> Path:
    override = os.environ.get(COURSES_DIR_ENV)
    return Path(override) if override else DEFAULT_COURSES_DIR


def course_slug(name: str) -> str:
    """A filesystem-safe stem for a course name.

    Course names come from Canvas, so they are outside our control; this keeps
    a profile file inside the courses directory no matter what they contain.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug[:80] or "course"


@dataclass
class CourseProfile:
    """One course's accumulated knowledge."""

    name: str
    path: Path
    body: str

    def exists(self) -> bool:
        return self.path.exists()


class CourseProfiles:
    """Read and write course profiles under one directory."""

    def __init__(self, directory: Optional[Path] = None):
        self.directory = Path(directory) if directory else default_courses_dir()

    def path_for(self, course_name: str) -> Path:
        return self.directory / f"{course_slug(course_name)}.md"

    def load(self, course_name: str) -> CourseProfile:
        """Load a profile, or hand back the empty template if none exists yet."""
        path = self.path_for(course_name)
        if path.exists():
            try:
                body = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                body = PROFILE_TEMPLATE.format(name=course_name)
        else:
            body = PROFILE_TEMPLATE.format(name=course_name)
        return CourseProfile(name=course_name, path=path, body=body)

    def save(self, course_name: str, body: str) -> Path:
        """Write a profile atomically, so a crash mid-write can't shred it."""
        import tempfile

        path = self.path_for(course_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".course-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(body if body.endswith("\n") else body + "\n")
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return path

    def known(self) -> list[str]:
        """Course slugs that already have a profile."""
        if not self.directory.exists():
            return []
        return sorted(p.stem for p in self.directory.glob("*.md"))
