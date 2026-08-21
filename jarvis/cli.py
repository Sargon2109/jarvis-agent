"""Fast, offline command line for Jarvis's plate.

Every command here talks straight to the store — no model, no API, instant and
free. It's how you check and manage what's on your plate day to day; the LLM
``dump`` flow (``main.py``) is only for when you want Jarvis to route a messy
brain-dump for you.

Run it as ``python -m jarvis <command>``. Examples:

    python -m jarvis add "finish econ pset" --kind task --domain homework
    python -m jarvis remind "book club room" --due 2026-08-09
    python -m jarvis agenda
    python -m jarvis list --domain startup
    python -m jarvis done a1b2c3d4
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from .agenda import build_agenda, render_agenda
from .agents import RESERVED_NAMES
from .briefing import DEFAULT_STALE_DAYS, build_briefing, render_briefing
from .canvas import (
    DEFAULT_WITHIN_DAYS as CANVAS_WITHIN_DAYS,
    CanvasClient,
    CanvasConfig,
    CanvasError,
    sync_to_store,
)
from .dumps import DumpLog, DumpLogError
from .models import KINDS, STATUSES, format_item
from .orchestrator import available_agents
from .promotion import (
    DEFAULT_THRESHOLD as PROMOTION_THRESHOLD,
    draft_description,
    draft_prompt,
    find_candidate,
    find_candidates,
    render_candidates,
)
from .registry import AgentRegistry, RegistryError
from .doctor import run_doctor
from .notify import NotifyState, run_notify
from .scheduling import DEFAULT_TIME, build_notify_plan, build_plan
from .server import DEFAULT_HOST, DEFAULT_PORT, JarvisAPI, serve
from .storage import Store, StoreError, create_store, migrate_items


def _load_env() -> None:
    """Load .env so CLI commands see JARVIS_CANVAS_* and ANTHROPIC_API_KEY,
    matching main.py. A missing python-dotenv is non-fatal — the offline
    commands don't need it."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:  # noqa: BLE001 - env loading is best-effort
        pass


def _use_utf8_stdout() -> None:
    """Stored titles can contain any Unicode (the model writes em-dashes and the
    user can type anything). The Windows console defaults to a legacy codepage
    that mangles them, so force UTF-8 where the stream supports it."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="Manage what's on your plate — offline, no API.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list items (hides done unless --all/--status)")
    p_list.add_argument("--kind", choices=KINDS)
    p_list.add_argument("--status", choices=STATUSES)
    p_list.add_argument("--domain")
    p_list.add_argument("--all", action="store_true", help="include finished items")

    p_add = sub.add_parser("add", help="capture a task or project")
    p_add.add_argument("text", help="what the item is")
    p_add.add_argument("--kind", choices=KINDS, default="task")
    p_add.add_argument("--domain", default="other")
    p_add.add_argument("--due", help="optional due date, YYYY-MM-DD")
    p_add.add_argument("--agent", help="optional owning agent")

    p_remind = sub.add_parser("remind", help="add a time-based reminder")
    p_remind.add_argument("text", help="what to be reminded of")
    p_remind.add_argument("--due", required=True, help="when, as YYYY-MM-DD")
    p_remind.add_argument("--domain", default="other")

    sub.add_parser("agenda", help="show overdue / today / upcoming")

    p_done = sub.add_parser("done", help="mark an item done by id")
    p_done.add_argument("id")

    p_remove = sub.add_parser("remove", help="delete an item by id")
    p_remove.add_argument("id")

    p_dump = sub.add_parser("dump", help="save a raw brain-dump verbatim (no API, no parsing)")
    p_dump.add_argument("text", help="whatever's on your mind")

    p_dumps = sub.add_parser("dumps", help="browse past brain-dumps")
    p_dumps.add_argument("--show", metavar="ID", help="print one dump in full, with the items it produced")
    p_dumps.add_argument("--limit", type=int, default=10, help="how many to list (default 10)")

    p_agents = sub.add_parser("agents", help="list the specialists Jarvis can delegate to")
    p_agents.add_argument("--show", metavar="NAME", help="print one agent's full prompt")

    p_forget = sub.add_parser("forget-agent", help="delete a custom agent by name")
    p_forget.add_argument("name")

    p_promote = sub.add_parser("promote", help="give a recurring area its own specialist")
    p_promote.add_argument("domain", nargs="?", help="the domain to promote (omit to see candidates)")
    p_promote.add_argument("--name", help="name for the new agent (defaults to the domain)")
    p_promote.add_argument("--threshold", type=int, default=PROMOTION_THRESHOLD,
                           help=f"mentions required before promoting (default {PROMOTION_THRESHOLD})")
    p_promote.add_argument("--force", action="store_true",
                           help="promote even if the domain hasn't hit the threshold")

    p_brief = sub.add_parser("brief", help="the full briefing: due, forgotten, unsorted, and suggestions")
    p_brief.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS,
                         help=f"days untouched before an item counts as forgotten (default {DEFAULT_STALE_DAYS})")

    p_sched = sub.add_parser("schedule", help="show how to run the briefing (or notifications) automatically")
    p_sched.add_argument("--at", default=DEFAULT_TIME, help=f"time of day, HH:MM (default {DEFAULT_TIME})")
    p_sched.add_argument("--job", choices=("brief", "notify"), default="brief",
                         help="brief = daily text briefing; notify = recurring desktop notifications")
    p_sched.add_argument("--every", type=int, default=60, metavar="MIN",
                         help="notify only: minutes between checks (default 60)")

    p_notify = sub.add_parser("notify", help="send desktop notifications for overdue/due-today items")
    p_notify.add_argument("--dry-run", action="store_true",
                          help="show what would be sent without sending or recording")

    sub.add_parser("doctor", help="check every data file for corruption; reports, never repairs")

    p_migrate = sub.add_parser("migrate-store",
                               help="copy all items to a new store file (e.g. plate.db for SQLite)")
    p_migrate.add_argument("target", help="path of the new store (.db/.sqlite = SQLite backend)")

    p_canvas = sub.add_parser("canvas", help="import upcoming Canvas assignments as homework reminders")
    p_canvas.add_argument("--within", type=int, default=CANVAS_WITHIN_DAYS, metavar="DAYS",
                          help=f"how many days ahead to import (default {CANVAS_WITHIN_DAYS})")
    p_canvas.add_argument("--dry-run", action="store_true",
                          help="show what would be imported without changing the plate")

    p_serve = sub.add_parser("serve", help="open the command desk (chat dashboard) in your browser")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    p_serve.add_argument("--host", default=DEFAULT_HOST,
                         help=f"bind address (default {DEFAULT_HOST}; loopback on purpose)")
    p_serve.add_argument("--no-browser", action="store_true", help="don't open a browser tab")

    return parser


# --- command handlers (each returns a process exit code) ---------------------

def _cmd_list(store: Store, args: argparse.Namespace) -> int:
    items = store.list(kind=args.kind, status=args.status, domain=args.domain)
    if not (args.all or args.status):
        items = [item for item in items if item.status != "done"]
    if not items:
        print("Nothing on your plate that matches.")
        return 0
    for item in items:
        print(format_item(item))
    return 0


def _cmd_add(store: Store, args: argparse.Namespace) -> int:
    item = store.add(
        args.text, kind=args.kind, domain=args.domain, due=args.due, agent=args.agent
    )
    print(f"Captured {format_item(item)}")
    return 0


def _cmd_remind(store: Store, args: argparse.Namespace) -> int:
    item = store.add(args.text, kind="reminder", domain=args.domain, due=args.due)
    print(f"Reminder set {format_item(item)}")
    return 0


def _cmd_agenda(store: Store, args: argparse.Namespace) -> int:
    print(render_agenda(build_agenda(store.all())))
    return 0


def _cmd_done(store: Store, args: argparse.Namespace) -> int:
    item = store.complete(args.id)
    if item is None:
        print(f"No item found with id {args.id!r}.")
        return 1
    print(f"Done: {format_item(item)}")
    return 0


def _cmd_remove(store: Store, args: argparse.Namespace) -> int:
    if store.remove(args.id):
        print(f"Removed {args.id}.")
        return 0
    print(f"No item found with id {args.id!r}.")
    return 1


def _cmd_dump(store: Store, args: argparse.Namespace, log: DumpLog) -> int:
    record = log.append(args.text, source="cli")
    print(f"Saved dump [{record.id}]. Run `python main.py` to have Jarvis sort it.")
    return 0


def _cmd_dumps(store: Store, args: argparse.Namespace, log: DumpLog) -> int:
    if args.show:
        record = log.get(args.show)
        if record is None:
            print(f"No dump with id {args.show!r}. Try: jarvis dumps")
            return 1
        print(f"[{record.id}] {record.created_at} ({record.source})\n")
        print(record.text)
        produced = [i for i in store.all() if i.dump_id == record.id]
        if produced:
            print(f"\nProduced {len(produced)} item(s):")
            for item in produced:
                print(f"  {format_item(item)}")
        else:
            print("\nNot yet sorted into items.")
        return 0

    records = log.recent(args.limit)
    if not records:
        print("No dumps recorded yet.")
        return 0
    # A dump counts as sorted once any item points back to it.
    sorted_ids = {i.dump_id for i in store.all() if i.dump_id}
    for record in records:
        mark = " " if record.id in sorted_ids else "*"
        print(f"{mark}[{record.id}] {record.created_at[:10]} {record.summary()}")
    if any(r.id not in sorted_ids for r in records):
        print("\n* = not yet sorted into items")
    return 0


def _cmd_agents(store: Store, args: argparse.Namespace, registry: AgentRegistry) -> int:
    agents, domain_map = available_agents(registry)
    custom = {r.name for r in registry.records()}
    by_agent = {name: domain for domain, name in domain_map.items()}

    if args.show:
        definition = agents.get(args.show)
        if definition is None:
            print(f"No agent named {args.show!r}. Try: jarvis agents")
            return 1
        print(f"{args.show}\n{'-' * len(args.show)}")
        print(f"tools: {', '.join(definition.tools or [])}\n")
        print(definition.prompt)
        return 0

    for name in sorted(agents):
        origin = "custom" if name in custom else "built-in"
        domain = by_agent.get(name)
        label = f" [{domain}]" if domain else ""
        summary = (agents[name].description or "").split(".")[0]
        print(f"{name}{label} ({origin}) - {summary}")
    return 0


def _cmd_forget_agent(store: Store, args: argparse.Namespace, registry: AgentRegistry) -> int:
    if registry.remove(args.name):
        print(f"Forgot agent {args.name!r}.")
        return 0
    print(f"No custom agent named {args.name!r} (built-ins can't be removed).")
    return 1


def _cmd_promote(store: Store, args: argparse.Namespace, registry: AgentRegistry) -> int:
    _, domain_map = available_agents(registry)
    items = store.all()

    if not args.domain:
        candidates = find_candidates(items, domain_map, threshold=args.threshold)
        print(render_candidates(candidates))
        if candidates:
            print("\nGive one its own specialist with: jarvis promote <domain>")
        return 0

    domain = args.domain.strip().lower()
    if domain in domain_map:
        print(f"'{domain}' already has a specialist: {domain_map[domain]}")
        return 1

    candidate = find_candidate(items, domain_map, domain, threshold=args.threshold)
    if candidate is None and not args.force:
        seen = sum(1 for i in items if (i.domain or "").lower() == domain)
        print(
            f"'{domain}' has come up {seen} time(s) — under the threshold of "
            f"{args.threshold}. Use --force to promote it anyway."
        )
        return 1

    titles = candidate.titles if candidate else []
    count = candidate.count if candidate else 0
    try:
        record = registry.add(
            name=(args.name or domain),
            description=draft_description(domain),
            prompt=draft_prompt(domain, titles),
            domain=domain,
            reason=f"promoted from the CLI after {count} items",
        )
    except RegistryError as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Created the '{record.name}' specialist for '{domain}'.")
    print(f"Review or sharpen its prompt with: jarvis agents --show {record.name}")
    return 0


def _cmd_brief(store: Store, args: argparse.Namespace, registry: AgentRegistry,
               log: DumpLog) -> int:
    _, domain_map = available_agents(registry)
    briefing = build_briefing(
        store.all(), log.all(), domain_map, stale_days=args.stale_days
    )
    print(render_briefing(briefing, stale_days=args.stale_days))
    return 0


def _cmd_schedule(store: Store, args: argparse.Namespace) -> int:
    if args.job == "notify":
        print(build_notify_plan(every_minutes=args.every).render())
    else:
        print(build_plan(args.at).render())
    return 0


def _cmd_notify(store: Store, args: argparse.Namespace) -> int:
    """Send (or preview) the due/overdue notifications."""
    result = run_notify(store.all(), state=NotifyState(), dry_run=args.dry_run)
    print(result.summary())
    if args.dry_run and result.sent:
        print("\n(dry run - nothing was sent or recorded)")
    return 0


def _cmd_doctor(store: Store, args: argparse.Namespace) -> int:
    report = run_doctor(reserved=RESERVED_NAMES)
    print(report.render())
    return 0 if report.ok() else 1


def _cmd_migrate_store(store: Store, args: argparse.Namespace) -> int:
    target = create_store(args.target)
    count = migrate_items(store, target)
    print(f"Copied {count} item(s) to {args.target}.")
    print(f"Point Jarvis at it with:  JARVIS_STORE_PATH={args.target}")
    return 0


def _cmd_canvas(store: Store, args: argparse.Namespace) -> int:
    """Pull upcoming assignments from Canvas onto the plate."""
    try:
        client = CanvasClient(CanvasConfig.from_env())
        result = sync_to_store(store, client, within_days=args.within, dry_run=args.dry_run)
    except CanvasError as exc:
        print(f"Error: {exc}")
        return 1
    print(result.summary())
    if args.dry_run and result.added:
        print("\n(dry run — nothing was added. Re-run without --dry-run to import.)")
    return 0


def _cmd_serve(store: Store, args: argparse.Namespace, registry: AgentRegistry,
               log: DumpLog) -> int:
    """Run the command desk. Blocks until Ctrl-C."""
    serve(
        host=args.host,
        port=args.port,
        api=JarvisAPI(store, registry, log),
        open_browser=not args.no_browser,
    )
    return 0


_HANDLERS = {
    "list": _cmd_list,
    "add": _cmd_add,
    "remind": _cmd_remind,
    "agenda": _cmd_agenda,
    "done": _cmd_done,
    "remove": _cmd_remove,
    "schedule": _cmd_schedule,
    "canvas": _cmd_canvas,
    "notify": _cmd_notify,
    "doctor": _cmd_doctor,
    "migrate-store": _cmd_migrate_store,
}

#: Commands that operate on the agent registry rather than the store.
_REGISTRY_HANDLERS = {
    "agents": _cmd_agents,
    "forget-agent": _cmd_forget_agent,
    "promote": _cmd_promote,
}

#: Commands that operate on the dump log.
_LOG_HANDLERS = {
    "dump": _cmd_dump,
    "dumps": _cmd_dumps,
}

#: Commands that need the store, the registry, and the dump log together.
_FULL_HANDLERS = {"brief": _cmd_brief, "serve": _cmd_serve}


def main(
    argv: Optional[Sequence[str]] = None,
    store: Optional[Store] = None,
    registry: Optional[AgentRegistry] = None,
    log: Optional[DumpLog] = None,
) -> int:
    """CLI entry point. Returns a process exit code.

    ``store``, ``registry`` and ``log`` can be injected for testing; otherwise
    the defaults (or their ``$JARVIS_*_PATH`` overrides) are used.
    """
    _use_utf8_stdout()
    _load_env()
    args = _build_parser().parse_args(argv)
    store = store or create_store()
    try:
        if args.command in _FULL_HANDLERS:
            return _FULL_HANDLERS[args.command](
                store,
                args,
                registry or AgentRegistry(reserved=RESERVED_NAMES),
                log or DumpLog(),
            )
        if args.command in _REGISTRY_HANDLERS:
            registry = registry or AgentRegistry(reserved=RESERVED_NAMES)
            return _REGISTRY_HANDLERS[args.command](store, args, registry)
        if args.command in _LOG_HANDLERS:
            return _LOG_HANDLERS[args.command](store, args, log or DumpLog())
        return _HANDLERS[args.command](store, args)
    except (ValueError, StoreError, RegistryError, DumpLogError, CanvasError) as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
