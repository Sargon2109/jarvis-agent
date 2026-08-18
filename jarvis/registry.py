"""The agent registry — specialists that persist between runs.

Built-in agents live in code (see :mod:`jarvis.agents`). Agents created later —
because a new area of the user's life turned out to recur — live here, as data in
``data/agents.json``, and are loaded back into real
:class:`~claude_agent_sdk.AgentDefinition` objects at startup.

This works because an agent *is* data: a description, a prompt, and a tool list.
Nothing is generated as code, so nothing arbitrary is ever executed.

Two safety rules are enforced on every custom agent:
  * tools must come from :data:`jarvis.agents.base.ALLOWED_TOOLS`;
  * a custom agent may never shadow a built-in name.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from claude_agent_sdk import AgentDefinition

from .agents.base import ALLOWED_TOOLS, FILE_TOOLS, STORE_TOOLS, build_agent

#: Fallback location: <repo>/data/agents.json (the data/ dir is gitignored).
DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "data" / "agents.json"

#: Env var to override where the registry lives.
REGISTRY_PATH_ENV = "JARVIS_REGISTRY_PATH"


def default_registry_path() -> Path:
    """Where the registry lives: ``$JARVIS_REGISTRY_PATH`` if set, else default."""
    override = os.environ.get(REGISTRY_PATH_ENV)
    return Path(override) if override else DEFAULT_REGISTRY_PATH


class RegistryError(RuntimeError):
    """Raised when the registry can't be read, or an agent record is invalid."""


@dataclass
class AgentRecord:
    """The serializable form of a specialist agent."""

    name: str
    description: str
    prompt: str
    domain: Optional[str] = None          # the Item.domain this agent owns
    tools: list[str] = field(default_factory=lambda: list(FILE_TOOLS))
    model: str = "sonnet"
    created_at: str = ""
    reason: str = ""                      # why this agent was created

    def to_definition(self) -> AgentDefinition:
        """Rebuild a live AgentDefinition, re-applying shared conventions."""
        file_tools = tuple(t for t in self.tools if t not in STORE_TOOLS)
        return build_agent(
            description=self.description,
            prompt=self.prompt,
            tools=file_tools,
            with_store=any(t in STORE_TOOLS for t in self.tools),
            model=self.model,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "AgentRecord":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def validate_record(record: AgentRecord, *, reserved: frozenset[str]) -> None:
    """Raise :class:`RegistryError` if a record is unsafe or malformed."""
    if not record.name or not record.name.replace("_", "").replace("-", "").isalnum():
        raise RegistryError(
            f"agent name must be alphanumeric (dashes/underscores ok), got {record.name!r}"
        )
    if record.name in reserved:
        raise RegistryError(f"{record.name!r} is a built-in agent and can't be replaced")
    if not record.description.strip() or not record.prompt.strip():
        raise RegistryError("agent needs both a description and a prompt")
    unknown = set(record.tools) - ALLOWED_TOOLS
    if unknown:
        raise RegistryError(f"tools outside the allowlist: {sorted(unknown)}")


class AgentRegistry:
    """JSON-backed collection of custom :class:`AgentRecord`s."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        reserved: frozenset[str] = frozenset(),
    ):
        self.path = Path(path) if path is not None else default_registry_path()
        #: Names that custom agents may not use (the built-ins).
        self.reserved = reserved
        # Same read-modify-write hazard as JSONStore, same cure.
        self._lock = threading.RLock()

    # --- low-level read / write ---------------------------------------------
    def _read(self) -> list[AgentRecord]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                doc = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise RegistryError(f"Could not read registry at {self.path}: {exc}") from exc
        try:
            return [AgentRecord.from_dict(d) for d in doc.get("agents", [])]
        except TypeError as exc:
            raise RegistryError(f"Malformed agent record in {self.path}: {exc}") from exc

    def _write(self, records: list[AgentRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "schema_version": self.SCHEMA_VERSION,
            "agents": [asdict(r) for r in records],
        }
        fd, tmp_path = tempfile.mkstemp(
            dir=self.path.parent, prefix=".agents-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # --- operations ----------------------------------------------------------
    def records(self) -> list[AgentRecord]:
        """All custom agent records."""
        return self._read()

    def get(self, name: str) -> Optional[AgentRecord]:
        return next((r for r in self._read() if r.name == name), None)

    def add(
        self,
        name: str,
        description: str,
        prompt: str,
        *,
        domain: Optional[str] = None,
        tools: Optional[list[str]] = None,
        model: str = "sonnet",
        reason: str = "",
    ) -> AgentRecord:
        """Create and persist a custom agent. Validates before writing."""
        record = AgentRecord(
            name=name,
            description=description,
            prompt=prompt,
            domain=domain,
            tools=list(tools) if tools is not None else list(FILE_TOOLS) + list(STORE_TOOLS),
            model=model,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            reason=reason,
        )
        validate_record(record, reserved=self.reserved)
        # Fail before persisting if the tools can't produce a valid definition.
        record.to_definition()
        with self._lock:
            records = self._read()
            if any(r.name == name for r in records):
                raise RegistryError(f"an agent named {name!r} already exists")
            records.append(record)
            self._write(records)
        return record

    def remove(self, name: str) -> bool:
        """Delete a custom agent. Returns True if something was removed."""
        with self._lock:
            records = self._read()
            kept = [r for r in records if r.name != name]
            if len(kept) == len(records):
                return False
            self._write(kept)
        return True

    def definitions(self) -> dict[str, AgentDefinition]:
        """Live AgentDefinitions for every valid custom record.

        A record that fails to build is skipped rather than crashing the run —
        one bad agent must never stop Jarvis from starting.
        """
        built: dict[str, AgentDefinition] = {}
        for record in self._read():
            if record.name in self.reserved:
                continue
            try:
                built[record.name] = record.to_definition()
            except (ValueError, RegistryError):
                continue
        return built

    def domain_map(self) -> dict[str, str]:
        """domain label -> agent name, for custom agents that claim a domain."""
        return {r.domain: r.name for r in self._read() if r.domain}
