"""The command desk — a local web server over the live store.

This is the dashboard the roadmap called for, and it is chat-first: you talk to
Jarvis the way you would in any chat client, and the orchestrator runs behind it,
capturing and delegating exactly as ``main.py`` does. The plate and the agenda
render live beside the conversation, so you watch your own words turn into
tracked work as it happens.

Design choices:

* **Stdlib only.** ``ThreadingHTTPServer`` and hand-rolled SSE, no Flask, no
  FastAPI, no build step. The rest of the package earns its keep without
  dependencies and the dashboard shouldn't be the thing that breaks that.
* **Loopback only.** The orchestrator behind this endpoint can write files and
  spend money. Binding it to a network interface would hand that to anyone on
  the network, so the default is ``127.0.0.1`` and anything else demands an
  explicit, loud opt-in.
* **The API is separable from the transport.** :class:`JarvisAPI` holds the
  store, registry, and dump log and returns plain dicts; the HTTP handler is a
  thin shell over it. That keeps the endpoints testable offline, with injected
  temp-file backends, in the same style as the CLI.
* **Streaming is the point.** A dump can take minutes and fan out to six
  specialists. Waiting on a spinner for that would waste the most interesting
  thing Jarvis does, so every message is pushed to the browser as it arrives.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import unquote, urlparse

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from .agenda import build_agenda
from .agents import RESERVED_NAMES
from .briefing import DEFAULT_STALE_DAYS, build_briefing
from .canvas import (
    DEFAULT_WITHIN_DAYS as CANVAS_WITHIN_DAYS,
    CanvasClient,
    CanvasConfig,
    CanvasError,
    sync_to_store,
)
from .dumps import DumpLog, DumpLogError
from .ledger import CostLedger
from .models import Item
from .orchestrator import available_agents, build_orchestrator_options
from .registry import AgentRegistry, RegistryError
from .storage import Store, StoreError, create_store
from .tools import SERVER_NAME

#: Where the single-page front end lives.
WEB_DIR = Path(__file__).resolve().parent / "web"

#: Default bind address. Loopback on purpose — see the module docstring.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Where the specialists write their drafts (see agents/base.py conventions).
DEFAULT_SCRATCH_DIR = Path(__file__).resolve().parent.parent / "scratch"

#: Host/Origin names that count as "this machine's browser".
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})

#: Cap on request bodies. A dump is text you typed; a megabyte is a novel.
MAX_BODY_BYTES = 1_000_000

#: MCP tool-name prefix to strip when labelling store calls in the feed.
_TOOL_PREFIX = f"mcp__{SERVER_NAME}__"


# --- serialization -----------------------------------------------------------

def _item_json(item: Item) -> dict:
    """An item as the front end wants it: stored fields plus derived age."""
    data = item.to_dict()
    data["age_days"] = item.age_days()
    return data


def _items_json(items: Iterable[Item]) -> list[dict]:
    return [_item_json(item) for item in items]


# --- the API -----------------------------------------------------------------

class JarvisAPI:
    """The dashboard's operations, independent of HTTP.

    ``store``, ``registry`` and ``log`` can be injected (tests do this);
    otherwise the standard locations — or their ``$JARVIS_*_PATH`` overrides —
    are used, so the dashboard, the CLI, and the agents all share one spine.
    """

    def __init__(
        self,
        store: Optional[Store] = None,
        registry: Optional[AgentRegistry] = None,
        log: Optional[DumpLog] = None,
        ledger: Optional[CostLedger] = None,
        scratch_dir: Path | str | None = None,
    ):
        self.store = store or create_store()
        self.registry = registry or AgentRegistry(reserved=RESERVED_NAMES)
        self.log = log or DumpLog()
        self.ledger = ledger or CostLedger()
        self.scratch_dir = Path(scratch_dir) if scratch_dir is not None else DEFAULT_SCRATCH_DIR
        #: One chat turn at a time. Two orchestrator runs at once would
        #: interleave confusingly even now that the store itself is locked.
        self._chat_lock = threading.Lock()
        #: The SDK session this desk is continuing. One desk = one conversation;
        #: without this every message would be a fresh amnesiac query() and
        #: "you agreed earlier in this conversation" could never be true.
        self._session_id: Optional[str] = None

    # --- read ----------------------------------------------------------------
    def state(self, *, stale_days: int = DEFAULT_STALE_DAYS) -> dict:
        """One snapshot with everything the desk renders."""
        agents, domain_map = available_agents(self.registry)
        items = self.store.all()
        dumps = self.log.all()
        briefing = build_briefing(items, dumps, domain_map, stale_days=stale_days)
        custom = {record.name for record in self.registry.records()}
        by_agent = {name: domain for domain, name in domain_map.items()}

        return {
            "items": _items_json(items),
            "agenda": {
                "overdue": _items_json(briefing.agenda.overdue),
                "today": _items_json(briefing.agenda.today),
                "upcoming": _items_json(briefing.agenda.upcoming),
            },
            "stale": _items_json(briefing.stale),
            "unsorted_dumps": [
                {"id": r.id, "created_at": r.created_at, "summary": r.summary(90)}
                for r in briefing.unsorted_dumps
            ],
            "candidates": [
                {"domain": c.domain, "count": c.count, "titles": c.titles}
                for c in briefing.candidates
            ],
            "agents": [
                {
                    "name": name,
                    "description": definition.description or "",
                    "domain": by_agent.get(name),
                    "origin": "custom" if name in custom else "built-in",
                }
                for name, definition in sorted(agents.items())
            ],
            "stats": {
                "active": briefing.active_count,
                "total": len(items),
                "done": sum(1 for i in items if i.status == "done"),
                "overdue": len(briefing.agenda.overdue),
                "today": len(briefing.agenda.today),
                "quiet": briefing.is_quiet(),
            },
            "costs": {
                "month": round(self.ledger.month_total(), 4),
                "total": round(self.ledger.total(), 4),
            },
            "session": {"active": self._session_id is not None},
            "today": date.today().isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    # --- write ---------------------------------------------------------------
    def add_item(self, payload: dict) -> dict:
        """Quick-add straight from the desk, with no model in the loop."""
        text = (payload.get("text") or "").strip()
        if not text:
            raise ValueError("an item needs some text")
        item = self.store.add(
            text,
            kind=(payload.get("kind") or "task"),
            domain=(payload.get("domain") or "other"),
            due=(payload.get("due") or None),
        )
        return {"item": _item_json(item)}

    def complete_item(self, item_id: str) -> dict:
        item = self.store.complete(item_id)
        if item is None:
            raise LookupError(f"no item with id {item_id!r}")
        return {"item": _item_json(item)}

    def reopen_item(self, item_id: str) -> dict:
        """Undo a completion — the desk makes mis-clicks likely, so make them cheap."""
        item = self.store.set_status(item_id, "active")
        if item is None:
            raise LookupError(f"no item with id {item_id!r}")
        return {"item": _item_json(item)}

    def remove_item(self, item_id: str) -> dict:
        if not self.store.remove(item_id):
            raise LookupError(f"no item with id {item_id!r}")
        return {"removed": item_id}

    def save_dump(self, text: str) -> dict:
        """Record a dump verbatim without running the model."""
        text = (text or "").strip()
        if not text:
            raise ValueError("nothing to save")
        record = self.log.append(text, source="cli")
        return {"dump": {"id": record.id, "created_at": record.created_at}}

    # --- system --------------------------------------------------------------
    def system_stats(self) -> dict:
        """Glanceable machine stats for the desk's widget strip. Stdlib only."""
        import shutil as _shutil

        load = None
        if hasattr(os, "getloadavg"):
            try:
                load = [round(x, 2) for x in os.getloadavg()]
            except OSError:
                load = None
        try:
            usage = _shutil.disk_usage(Path.home())
            disk = {
                "free_gb": round(usage.free / 1e9, 1),
                "total_gb": round(usage.total / 1e9, 1),
            }
        except OSError:
            disk = None
        return {"load": load, "disk": disk, "cpus": os.cpu_count()}

    # --- scratch -------------------------------------------------------------
    def scratch_files(self) -> dict:
        """The drafts the specialists have produced, newest first.

        This is where the actual work product lands, so the desk must be able
        to show it — otherwise the most expensive thing Jarvis does is invisible.
        """
        if not self.scratch_dir.is_dir():
            return {"files": []}
        files: list[dict] = []
        for path in self.scratch_dir.iterdir():
            if not path.is_file() or path.name.startswith("."):
                continue
            stat = path.stat()
            files.append({
                "name": path.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                            .isoformat(timespec="seconds"),
            })
        files.sort(key=lambda f: str(f["modified"]), reverse=True)
        return {"files": files}

    def scratch_file(self, name: str) -> dict:
        """One draft's content. The name must be a plain filename — no paths."""
        if (
            not name
            or name != Path(name).name   # rejects separators and '..'
            or name.startswith(".")
        ):
            raise LookupError(f"no scratch file named {name!r}")
        path = self.scratch_dir / name
        # Belt and braces: even a name that slipped the check above must
        # resolve to a direct child of the scratch dir.
        if (
            not path.is_file()
            or path.resolve().parent != self.scratch_dir.resolve()
        ):
            raise LookupError(f"no scratch file named {name!r}")
        return {
            "name": name,
            "content": path.read_text(encoding="utf-8", errors="replace"),
            "modified": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                        .isoformat(timespec="seconds"),
        }

    # --- canvas --------------------------------------------------------------
    def sync_canvas(self, *, within_days: int = CANVAS_WITHIN_DAYS,
                    dry_run: bool = False) -> dict:
        """Import upcoming Canvas assignments onto the plate. Raises CanvasError
        (not configured / auth / unreachable), which the handler maps to a 400."""
        client = CanvasClient(CanvasConfig.from_env())
        result = sync_to_store(self.store, client, within_days=within_days, dry_run=dry_run)
        return {
            "found": result.found,
            "skipped": result.skipped,
            "added": [
                {"title": a.title, "course": a.course, "due": a.due}
                for a in result.added
            ],
            "summary": result.summary(),
        }

    # --- session -------------------------------------------------------------
    def reset_session(self) -> dict:
        """Forget the current conversation. The next message starts fresh.

        Guarded by the chat lock: a reset landing mid-run would otherwise race
        the turn's own ``_session_id`` write and could resurrect a dead session
        or drop a live one. If a turn is in flight the reset is refused — the
        run owns the session until it ends.
        """
        if not self._chat_lock.acquire(blocking=False):
            return {"session": {"active": True}, "busy": True}
        try:
            self._session_id = None
        finally:
            self._chat_lock.release()
        return {"session": {"active": False}}

    # --- chat ----------------------------------------------------------------
    def stream_chat(self, prompt: str, emit: Callable[[dict], None]) -> None:
        """Run one orchestrator turn, pushing an event dict per SDK message.

        The dump is recorded *before* the model runs, matching ``main.py``: your
        words are safe even if the API call never returns.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            emit({"type": "error", "message": "Say something first."})
            return

        if not self._chat_lock.acquire(blocking=False):
            emit({
                "type": "error",
                "message": "Jarvis is still working on the previous request.",
            })
            return

        resuming = self._session_id
        try:
            dump = self.log.append(prompt, source="llm")
            emit({"type": "dump", "id": dump.id})
            emit({"type": "session", "resumed": resuming is not None})
            options = build_orchestrator_options(
                store=self.store,
                registry=self.registry,
                dump_id=dump.id,
                resume=resuming,
            )
            asyncio.run(self._arun(prompt, options, emit, dump_id=dump.id))
        except (DumpLogError, StoreError, RegistryError) as exc:
            emit({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surface it rather than hang the UI
            if resuming:
                # A stale or unloadable session must not wedge every later
                # turn; drop it so the next message starts a fresh conversation.
                self._session_id = None
            emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            self._chat_lock.release()
            emit({"type": "end"})

    async def _arun(
        self,
        prompt: str,
        options,
        emit: Callable[[dict], None],
        dump_id: Optional[str] = None,
    ) -> None:
        """Translate the SDK's message stream into front-end events."""
        # tool_use_id -> specialist name, so a subagent's output can be attributed
        # back to the delegation that started it.
        delegations: dict[str, str] = {}

        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                speaker = delegations.get(message.parent_tool_use_id or "")
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text = block.text.strip()
                        if text:
                            emit({"type": "text", "text": text, "agent": speaker})
                    elif isinstance(block, ToolUseBlock):
                        emit(self._tool_event(block, delegations, speaker))
            elif isinstance(message, ResultMessage):
                if message.session_id:
                    # Continue this conversation on the next turn.
                    self._session_id = message.session_id
                if message.total_cost_usd is not None:
                    try:
                        self.ledger.append(
                            message.total_cost_usd,
                            turns=message.num_turns,
                            duration_ms=message.duration_ms,
                            dump_id=dump_id,
                        )
                    except OSError:
                        pass  # bookkeeping must never kill a finished run
                emit({
                    "type": "result",
                    "cost": message.total_cost_usd,
                    "turns": message.num_turns,
                    "duration_ms": message.duration_ms,
                    "is_error": message.is_error,
                    "stopped": message.terminal_reason,
                    "month_cost": round(self.ledger.month_total(), 4),
                })

    def _tool_event(
        self,
        block: ToolUseBlock,
        delegations: dict[str, str],
        speaker: Optional[str],
    ) -> dict:
        """One tool call as a feed event, remembering delegations as they start."""
        if block.name == "Task":
            target = block.input.get("subagent_type") or "?"
            delegations[block.id] = target
            return {
                "type": "delegate",
                "agent": target,
                "task": block.input.get("description") or "",
                "parent": speaker,
            }
        if block.name.startswith(_TOOL_PREFIX):
            name = block.name[len(_TOOL_PREFIX):]
            return {
                "type": "tool",
                "tool": name,
                "scope": "store",
                "detail": self._tool_detail(name, block.input),
                "agent": speaker,
                # Any store write changes what the plate should show.
                "mutates": name not in ("list_plate", "agenda", "propose_agents"),
            }
        if block.name in ("WebFetch", "WebSearch"):
            return {
                "type": "tool",
                "tool": block.name,
                "scope": "web",
                "detail": str(block.input.get("url") or block.input.get("query") or ""),
                "agent": speaker,
                "mutates": False,
            }
        return {
            "type": "tool",
            "tool": block.name,
            "scope": "file",
            "detail": str(block.input.get("file_path") or block.input.get("pattern") or ""),
            "agent": speaker,
            "mutates": False,
        }

    @staticmethod
    def _tool_detail(name: str, payload: dict) -> str:
        """A short, human-readable argument summary for the live feed."""
        if name in ("capture_thought", "add_reminder"):
            title = payload.get("title") or payload.get("raw") or ""
            due = payload.get("due")
            return f"{title}{f' (due {due})' if due else ''}"
        if name == "complete_item":
            return payload.get("id") or ""
        if name == "list_plate":
            bits = [f"{k}={v}" for k, v in payload.items() if v]
            return ", ".join(bits)
        return ""


# --- HTTP transport ----------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Thin HTTP shell over :class:`JarvisAPI`."""

    api: JarvisAPI
    web_dir: Path
    server_version = "Jarvis"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # --- helpers -------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The desk is a live view; a cached snapshot would quietly lie.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # --- request authentication ----------------------------------------------
    # Loopback binding alone does not protect this server: the user's own
    # browser is on localhost, and any web page it visits can fire requests at
    # 127.0.0.1. Three checks close the browser-borne attacks:
    #   * Host must be loopback (or the bound address) — defeats DNS rebinding,
    #     where an attacker's hostname resolves to 127.0.0.1 and their page
    #     reads responses as same-origin.
    #   * Origin, when a browser sends one, must be this server — defeats
    #     cross-site requests from pages on other origins.
    #   * Mutations must be application/json — a cross-site "simple request"
    #     can only send text/plain or form types without a CORS preflight, and
    #     the preflight fails here because this server never answers it.

    def _bound_address(self) -> str:
        address = self.server.server_address
        return str(address[0]) if isinstance(address, tuple) else str(address)

    def _allowed_names(self) -> frozenset[str]:
        return LOOPBACK_NAMES | {self._bound_address()}

    def _host_ok(self) -> bool:
        bound = self._bound_address()
        if bound in ("0.0.0.0", "::"):
            # Bound to everything on explicit request: there is no single
            # correct Host value to insist on. serve() already warned loudly.
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):                       # [::1]:port
            name = host[1:].split("]", 1)[0]
        else:
            name = host.rsplit(":", 1)[0] if ":" in host else host
        return name in self._allowed_names()

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True   # not a browser (curl, scripts) — Host check still applies
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        name = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and name in self._allowed_names()

    def _reject_unauthorized(self, *, mutation: bool) -> bool:
        """Send a 403 and return True when the request must not proceed."""
        if not self._host_ok():
            self._error(403, "forbidden: unrecognized Host header")
            return True
        if not self._origin_ok():
            self._error(403, "forbidden: cross-origin requests are not allowed")
            return True
        if mutation:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._error(403, "forbidden: mutations must send application/json")
                return True
        return False

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib name
        """Quiet by default — the console is where the boot banner lives."""
        return

    # --- routes --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        if self._reject_unauthorized(mutation=False):
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_page()
        if path == "/api/state":
            try:
                return self._json(self.api.state())
            except (StoreError, RegistryError, DumpLogError) as exc:
                return self._error(500, str(exc))
        if path == "/api/system":
            return self._json(self.api.system_stats())
        if path == "/api/scratch":
            return self._json(self.api.scratch_files())
        if path.startswith("/api/scratch/"):
            name = unquote(path[len("/api/scratch/"):])
            try:
                return self._json(self.api.scratch_file(name))
            except LookupError as exc:
                return self._error(404, str(exc))
            except OSError as exc:
                return self._error(500, str(exc))
        return self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        if self._reject_unauthorized(mutation=True):
            return
        path = urlparse(self.path).path
        if path == "/api/chat":
            return self._serve_chat()
        try:
            if path == "/api/items":
                return self._json(self.api.add_item(self._body()))
            if path == "/api/dumps":
                return self._json(self.api.save_dump(self._body().get("text", "")))
            if path == "/api/session/reset":
                return self._json(self.api.reset_session())
            if path == "/api/canvas/sync":
                return self._json(self.api.sync_canvas())
            if path.startswith("/api/items/"):
                item_id, _, action = path[len("/api/items/"):].partition("/")
                item_id = unquote(item_id)
                if action == "done":
                    return self._json(self.api.complete_item(item_id))
                if action == "reopen":
                    return self._json(self.api.reopen_item(item_id))
                if action == "remove":
                    return self._json(self.api.remove_item(item_id))
        except LookupError as exc:
            return self._error(404, str(exc))
        except (ValueError, StoreError, RegistryError, DumpLogError, CanvasError) as exc:
            return self._error(400, str(exc))
        return self._error(404, "not found")

    # --- handlers ------------------------------------------------------------
    def _serve_page(self) -> None:
        page = self.web_dir / "index.html"
        try:
            body = page.read_bytes()
        except OSError:
            return self._error(500, f"front end missing at {page}")
        self._send(200, body, "text/html; charset=utf-8")

    def _serve_chat(self) -> None:
        """Stream one orchestrator turn to the browser as Server-Sent Events."""
        try:
            prompt = self._body().get("message", "")
        except ValueError as exc:
            return self._error(400, str(exc))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # No Content-Length is knowable up front, so the stream ends with the
        # connection. The client reads to EOF rather than reusing the socket.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        def emit(event: dict) -> None:
            frame = f"data: {json.dumps(event)}\n\n".encode("utf-8")
            self.wfile.write(frame)
            self.wfile.flush()

        try:
            self.api.stream_chat(prompt, emit)
        except (BrokenPipeError, ConnectionResetError):
            # The user closed the tab mid-run. The dump and every captured item
            # are already on disk, so there is nothing to recover.
            return


def build_handler(api: JarvisAPI, web_dir: Path = WEB_DIR):
    """A handler class bound to this API instance."""
    return type("JarvisHandler", (_Handler,), {"api": api, "web_dir": web_dir})


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    api: Optional[JarvisAPI] = None,
    *,
    open_browser: bool = True,
) -> None:
    """Run the command desk until interrupted."""
    api = api or JarvisAPI()
    httpd = ThreadingHTTPServer((host, port), build_handler(api))
    httpd.daemon_threads = True
    url = f"http://{host}:{port}"

    print("  JARVIS command desk")
    print(f"  {url}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "\n  WARNING: bound to a non-loopback address. Anyone who can reach\n"
            "  this port can spend your API credit and write files. Ctrl-C now\n"
            "  unless you meant it."
        )
    print("\n  Ctrl-C to stop.\n")

    if open_browser:
        # Best effort: a headless or unusual environment shouldn't stop the server.
        try:
            import webbrowser

            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        except Exception:  # noqa: BLE001 - opening a browser is a convenience
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Desk closed.")
    finally:
        httpd.server_close()
