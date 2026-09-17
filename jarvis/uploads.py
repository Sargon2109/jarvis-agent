"""Files the user hands to Jarvis.

Canvas covers what an instructor posts. This covers everything else: the rubric
that only exists as a Word attachment, a graded paper worth imitating, notes
from a classmate, a textbook chapter. Without it the only way to give Jarvis a
reference was to describe it, which is exactly the gap that made a drafted
paper miss a rubric it had never actually seen.

Uploads land in ``scratch/uploads/`` — inside the one directory specialists are
already allowed to read and write, so no new permission is involved. Word and
PowerPoint files get a text companion on arrival for the same reason Canvas
downloads do: the Read tool sees them as binary otherwise.

The bytes arrive base64-encoded inside a normal JSON body rather than as a
multipart form. That is not laziness — the desk refuses any mutation that is
not ``application/json``, because a cross-site form post can send multipart
without a preflight but cannot send JSON. Keeping uploads on the JSON path
keeps that defense intact.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

#: Where uploads live, relative to the scratch directory.
UPLOAD_DIRNAME = "uploads"

#: Largest single upload. Generous enough for a scanned reading, small enough
#: that a stray drag-and-drop can't fill the disk.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: Cap on the extracted-text companion, matching the Canvas downloads. The
#: agent reads the companion, so an uncapped one just moves a huge payload
#: into the model's context instead of onto the disk.
MAX_EXTRACT_CHARS = 250_000


class UploadError(Exception):
    """A upload that cannot be accepted, with a reason worth showing."""


def safe_filename(name: str) -> str:
    """Reduce a user-supplied filename to something safe to write.

    The name comes from a file picker, so it is whatever the filesystem it came
    from allowed — including separators and leading dots. Stripping those keeps
    every upload a direct child of the uploads directory.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", (name or "")).strip("._ -")
    return cleaned[:120] or "upload"


@dataclass
class Upload:
    """One stored file."""

    name: str
    path: Path
    size: int
    modified: str
    text_path: Optional[Path] = None   # extracted companion, when there is one

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": str(self.path),
            "size": self.size,
            "modified": self.modified,
            "text_path": str(self.text_path) if self.text_path else None,
        }


class UploadStore:
    """Reads and writes the user's uploaded reference files."""

    def __init__(self, scratch_dir: Path | str):
        self.directory = Path(scratch_dir) / UPLOAD_DIRNAME

    def save(self, filename: str, data: bytes) -> Upload:
        """Store one file, extracting text from Office formats on the way in."""
        if not data:
            raise UploadError("that file is empty")
        if len(data) > MAX_UPLOAD_BYTES:
            raise UploadError(
                f"that file is {len(data) // 1024 // 1024} MB; the limit is "
                f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB"
            )
        name = safe_filename(filename)
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / name

        handle, temp_name = tempfile.mkstemp(
            dir=str(self.directory), prefix=".upload-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
            os.replace(temp_name, target)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

        text_path = self._extract(target, data)
        stat = target.stat()
        return Upload(
            name=name,
            path=target,
            size=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                     .isoformat(timespec="seconds"),
            text_path=text_path,
        )

    def _extract(self, target: Path, data: bytes) -> Optional[Path]:
        """Write a .txt companion for formats the Read tool can't parse."""
        from .canvas_tools import OFFICE_SUFFIXES, office_to_text

        suffix = target.suffix.lower()
        if suffix not in OFFICE_SUFFIXES:
            return None
        try:
            text = office_to_text(suffix, data)
        except Exception:      # a malformed document must not fail the upload
            return None
        if not text:
            return None
        if len(text) > MAX_EXTRACT_CHARS:
            text = text[:MAX_EXTRACT_CHARS] + "\n\n[...truncated]"
        companion = target.with_suffix(suffix + ".txt")
        try:
            companion.write_text(text, encoding="utf-8")
        except OSError:
            return None
        return companion

    def all(self) -> list[Upload]:
        """Everything uploaded, newest first."""
        if not self.directory.is_dir():
            return []
        found: list[Upload] = []
        for path in self.directory.iterdir():
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.name.endswith(".txt") and path.with_suffix("").exists():
                continue          # a companion, not an upload in its own right
            stat = path.stat()
            companion = path.with_suffix(path.suffix + ".txt")
            found.append(Upload(
                name=path.name,
                path=path,
                size=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                         .isoformat(timespec="seconds"),
                text_path=companion if companion.exists() else None,
            ))
        found.sort(key=lambda u: u.modified, reverse=True)
        return found

    def resolve(self, name: str) -> Optional[Upload]:
        """One upload by name, or None. Rejects anything with a path in it."""
        if not name or name != Path(name).name or name.startswith("."):
            return None
        for upload in self.all():
            if upload.name == name:
                return upload
        return None

    def remove(self, name: str) -> bool:
        """Delete an upload and its companion. Returns whether it existed."""
        upload = self.resolve(name)
        if upload is None:
            return False
        for path in (upload.text_path, upload.path):
            if path is None:
                continue
            try:
                path.unlink()
            except OSError:
                pass
        return True


def describe_for_prompt(uploads: list[Upload]) -> str:
    """The note appended to a message so the agent knows what it was given.

    An uploaded file the agent is never told about is just wasted disk. The
    path matters more than the name — it is what Read takes — and the
    extracted companion is named explicitly so a .docx rubric is read as
    prose rather than reported as unreadable binary.
    """
    if not uploads:
        return ""
    lines = [
        "",
        "",
        "The user attached these files. Read them before answering — they are "
        "the reference for this request:",
    ]
    for upload in uploads:
        readable = upload.text_path or upload.path
        lines.append(f"  - {upload.name}: {readable}")
    return "\n".join(lines)
