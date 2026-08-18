"""Concurrency tests for the store and registry.

The command desk serves requests on threads; before the store grew its lock,
two parallel writers could each rewrite plate.json from their own stale read
and silently drop the other's items. These tests hammer the paths that raced.
"""

import tempfile
import threading
from pathlib import Path

from jarvis.agents import RESERVED_NAMES
from jarvis.registry import AgentRegistry
from jarvis.storage import JSONStore


def _store() -> JSONStore:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-conc-"))
    return JSONStore(tmp / "plate.json")


def _run_threads(workers):
    threads = [threading.Thread(target=w) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_parallel_adds_lose_nothing():
    store = _store()
    PER_THREAD, THREADS = 25, 4

    def adder(n):
        def run():
            for i in range(PER_THREAD):
                store.add(f"item {n}-{i}", kind="task", domain="club")
        return run

    _run_threads([adder(n) for n in range(THREADS)])
    assert len(store.all()) == PER_THREAD * THREADS


def test_parallel_mixed_mutations_stay_consistent():
    store = _store()
    seed = [store.add(f"seed {i}") for i in range(20)]

    def completer():
        for item in seed[:10]:
            store.complete(item.id)

    def remover():
        for item in seed[10:15]:
            store.remove(item.id)

    def adder():
        for i in range(10):
            store.add(f"new {i}")

    _run_threads([completer, remover, adder])
    items = store.all()
    assert len(items) == 20 - 5 + 10
    done = {i.id for i in items if i.status == "done"}
    assert done == {item.id for item in seed[:10]}


def test_parallel_registry_adds_keep_every_agent():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-conc-reg-"))
    registry = AgentRegistry(tmp / "agents.json", reserved=RESERVED_NAMES)

    def adder(n):
        def run():
            for i in range(5):
                registry.add(f"agent-{n}-{i}", "d.", "p.", domain=f"dom-{n}-{i}")
        return run

    _run_threads([adder(n) for n in range(3)])
    assert len(registry.records()) == 15
