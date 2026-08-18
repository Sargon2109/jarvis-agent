# Jarvis

A personal chief-of-staff agent. You dump your thoughts into it every now and
then; it remembers them, sorts them, reminds you what's on your plate, and
delegates the deeper work to specialist agents.

Built on [Anthropic's Claude Agent SDK](https://docs.claude.com).

---

## Three ways to use it

**1. The offline CLI — fast, free, no API.** Day-to-day management of your plate.

```bash
python -m jarvis add "finish econ pset" --kind task --domain homework
python -m jarvis remind "book the club room" --due 2026-08-09
python -m jarvis agenda        # what's overdue / today / upcoming
python -m jarvis list --domain startup
python -m jarvis done a1b2c3d4 # mark done by id
python -m jarvis remove a1b2c3d4

python -m jarvis dump "whatever's on my mind right now"   # save raw, sort later
python -m jarvis dumps         # browse past brain-dumps
python -m jarvis dumps --show f7e3b072

python -m jarvis brief         # the full picture, unprompted
python -m jarvis promote       # areas that have earned their own specialist
python -m jarvis promote debate
python -m jarvis schedule --at 07:00   # how to run the brief every morning

python -m jarvis agents        # who Jarvis can delegate to
python -m jarvis agents --show club
```

`dump` is the zero-friction capture: it saves your words verbatim and instantly,
with no model involved, so you can empty your head and let Jarvis sort it later.

**2. The LLM dump — routes a messy brain-dump for you.** Needs your API key.

```bash
python main.py "start a coding club, rebrand robotics, don't stress history hw,
                push startup outreach, grind leetcode, look at Jane Street"
```

The orchestrator breaks the dump into individual items, saves each with the
right kind/domain, tells you what's now on your plate, and delegates deeper work
to specialist subagents. Every run prints its own API cost.

**3. The command desk — the dashboard.** Same orchestrator, in a browser.

```bash
python -m jarvis serve            # http://127.0.0.1:8765
python -m jarvis serve --port 9000 --no-browser
```

Chat-first, like any chat client: you type a dump, and the orchestrator runs
behind it. The difference is that you *watch* it work — every capture, every
delegation, and every specialist's reply stream into the feed live (rendered as
markdown), while the plate re-renders beside the conversation as items land.
The desk holds **one continuous conversation** — follow-ups actually follow up,
because each turn resumes the same SDK session; the New Session button starts
fresh. A Files tab shows the drafts specialists write into `scratch/`, quick-add
puts items on the plate with no model involved, and the header tracks what this
session and this month have actually cost.

It's stdlib-only (`ThreadingHTTPServer` + server-sent events), so there's no
build step. It binds to `127.0.0.1` by design, and loopback alone isn't trusted:
every request must carry a loopback `Host` (stops DNS rebinding) and, for
mutations, a same-origin `Origin` plus `application/json` (stops cross-site
requests from pages your browser happens to have open — which would otherwise
be able to spend your API credit). Every run is capped by `max_turns` and
`max_budget_usd` (defaults 100 / $3.00; override with `JARVIS_MAX_TURNS` and
`JARVIS_MAX_BUDGET_USD`), so a runaway run stops instead of running a tab.

---

## Setup

```bash
python -m venv venv
venv\Scripts\activate            # Windows (use source venv/bin/activate on macOS/Linux)
pip install -r requirements.txt  # pinned; or `pip install -e .` for the `jarvis` command
copy .env.example .env           # then paste your ANTHROPIC_API_KEY (only needed for main.py)
```

Python 3.10+ is required (the Claude Agent SDK's floor).

---

## Architecture

Everything is a small, single-responsibility piece of the `jarvis/` package:

| Module | Responsibility |
| --- | --- |
| `jarvis/models.py` | The `Item` dataclass + validation and (de)serialization. |
| `jarvis/storage.py` | `Store` interface + `JSONStore` (atomic writes, corruption-safe). |
| `jarvis/agenda.py` | Buckets dated items into overdue / today / upcoming. |
| `jarvis/dumps.py` | Append-only log of every brain-dump, kept verbatim. |
| `jarvis/briefing.py` | The unprompted digest: due, forgotten, unsorted, suggested. |
| `jarvis/promotion.py` | Decides when a recurring area has earned a specialist. |
| `jarvis/scheduling.py` | Composes the morning-run command for your OS. |
| `jarvis/tools.py` | The store exposed to agents as in-process SDK tools. |
| `jarvis/orchestrator.py` | Wires tools + subagents + prompt into agent options. |
| `jarvis/agents/` | The specialists (see below). |
| `jarvis/registry.py` | Custom agents that persist between runs. |
| `jarvis/cli.py` | The offline command line. |
| `jarvis/server.py` | The command desk: JSON API + streamed chat over the live store. |
| `jarvis/ledger.py` | Append-only cost ledger — what every run actually spent. |
| `jarvis/web/` | The desk's single-page front end (no build step). |
| `main.py` | The LLM dump entry point. |

### The specialists

Items are routed by their `domain` to a matching specialist: **club**,
**rebrand**, **startup**, **homework**, **leetcode**, **internships**. Anything
else falls to the **generalist**, which is the guarantee that no thought is ever
dropped just because no specialist fits. (`researcher` and `writer` remain for
generic read-only and drafting subtasks.)

Every specialist can also reach the store, so it can record follow-ups it
discovers and close out the work it was handed.

An agent is just data — a description, a prompt, and a tool list — so new ones
can be added to `data/agents.json` and are loaded at startup. Three rules are
always enforced: a custom agent can't shadow a built-in name, its tools must come
from the allowlist (file tools + store tools; never arbitrary execution), and
**only the orchestrator can create agents** — a specialist doing its work can
never reshape the system that dispatches it.

### Agents are earned, not spawned

New specialists appear through *promotion*, not on first mention. When an area
recurs (three items by default) with no owner, it becomes a candidate:

```bash
python -m jarvis promote            # what's earned it
python -m jarvis promote debate     # give it a specialist
```

The alternative — spawning an agent the first time a new word appears — produces
dozens of one-off specialists with thin prompts, which makes routing *worse*,
because the orchestrator then has to choose between many vague descriptions.
Requiring recurrence keeps the roster small and sharp. The orchestrator can also
suggest promotions mid-conversation, but it is instructed never to create one
without you saying yes.

### The morning briefing

`jarvis brief` answers the question you didn't think to ask — what's due, what
you've clearly forgotten, what you captured but never sorted, and which areas
have earned a specialist. It's pure local computation, so it costs nothing.

`jarvis schedule` prints the exact command to run it every morning (Task
Scheduler on Windows, cron on macOS/Linux). It **prints** the command rather than
running it — registering a background job is a change to your machine, and that
stays your decision.

The **store is the spine**: both the CLI and the agents read and write the same
`data/plate.json`. The `Store` interface means a future SQLite backend can drop
in without touching anything else.

### The command desk

`jarvis serve` is the same system with a face on it. Three panels: the specialist
roster on the left (each one lights up while it's actually working), the
conversation in the middle, and the live plate on the right.

The interesting part is the attribution. The SDK tags every streamed message with
the `parent_tool_use_id` of the delegation that produced it, so the desk can show
*which specialist is talking* rather than one undifferentiated wall of output —
you see the club agent and the startup agent working in parallel, each under its
own heading.

Chat turns are serialized behind a lock, and the store itself now locks every
read-modify-write — the desk's threaded server means quick-adds, item clicks,
and the orchestrator's own captures can all write at once, and none of them may
lose another's work. (`tests/test_concurrency.py` hammers exactly this.)

### Your data

Everything is local files under `data/` (gitignored — your thoughts never go to
GitHub):

| File | What's in it | Override with |
| --- | --- | --- |
| `plate.json` | The items Jarvis parsed out of your dumps. | `JARVIS_STORE_PATH` |
| `dumps.jsonl` | Every brain-dump, verbatim, append-only. | `JARVIS_DUMPS_PATH` |
| `agents.json` | Custom agents added over time. | `JARVIS_REGISTRY_PATH` |
| `costs.jsonl` | One line per run: cost, turns, duration, dump id. | `JARVIS_LEDGER_PATH` |

Items carry the `dump_id` they came from, so any task can be traced back to what
you actually said — and a dump counts as "sorted" once items point at it.

Scale is not a concern: at 5,000 items the store is ~2 MB and every operation
runs in under 100 ms, which is roughly a decade of weekly dumps. If it ever does
outgrow a JSON file, `Store` is an interface — a SQLite backend drops in without
touching the tools, agents, or CLI.

**Backup:** these are ordinary files. Point the env vars at a synced folder
(OneDrive, Dropbox) if you want them backed up automatically.

---

## Tests

No pytest required:

```bash
python run_tests.py
```

All tests run fully offline (no API calls).

---

## Roadmap

1. **Memory spine** — persistence + tools so nothing is forgotten. ✅
2. **Intake** — reliable capture from a dump; the offline CLI + agenda. ✅ (this phase)
3. **Domain agents** — specialists per area, a generalist catch-all, a persistent
   registry, and promotion so new agents are earned. ✅
4. **Automation + reminders** — the briefing and the scheduling command. ✅
5. **Frontend** — the "command desk" dashboard over the live store. ✅

Still ahead: having the morning run *act* rather than only report — drafting the
work that doesn't need you, so the briefing arrives with the easy things already
done.
