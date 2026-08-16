"""Zero-dependency test runner (no pytest needed).

Discovers every ``test_*`` function in the tests package, runs it (awaiting it
if it's async), and prints a PASS/FAIL summary. Exits non-zero on any failure.

Usage:  python run_tests.py
"""

import asyncio
import importlib
import inspect
import traceback

TEST_MODULES = [
    "tests.test_models",
    "tests.test_store",
    "tests.test_agenda",
    "tests.test_dumps",
    "tests.test_promotion",
    "tests.test_briefing",
    "tests.test_scheduling",
    "tests.test_tools",
    "tests.test_agents",
    "tests.test_registry",
    "tests.test_agent_tools",
    "tests.test_cli",
]


def _collect(module):
    return [
        (f"{module.__name__}.{name}", fn)
        for name, fn in vars(module).items()
        if name.startswith("test_") and inspect.isfunction(fn)
    ]


def main() -> int:
    passed, failures = 0, []

    for module_name in TEST_MODULES:
        module = importlib.import_module(module_name)
        for name, fn in _collect(module):
            try:
                result = fn()
                if inspect.iscoroutine(result):
                    asyncio.run(result)
            except Exception:  # noqa: BLE001 - report everything
                failures.append((name, traceback.format_exc()))
                print(f"FAIL  {name}")
            else:
                passed += 1
                print(f"ok    {name}")

    print("\n" + "=" * 60)
    for name, tb in failures:
        print(f"\nFAILURE: {name}\n{tb}")
    print(f"{passed} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
