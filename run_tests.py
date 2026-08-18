"""Zero-dependency test runner (no pytest needed).

Discovers every ``test_*`` function in the tests package, runs it (awaiting it
if it's async), and prints a PASS/FAIL summary. Exits non-zero on any failure.

Usage:  python run_tests.py
"""

import asyncio
import importlib
import inspect
import sys
import traceback
from pathlib import Path

if not __debug__:
    # Under ``python -O`` every assert is stripped and the whole suite would
    # pass vacuously. Refuse to lie.
    sys.exit("run_tests.py must run without -O (asserts are the tests)")

#: Discovered, not listed: a new test file that nobody registers must still run.
TEST_MODULES = sorted(
    f"tests.{path.stem}"
    for path in (Path(__file__).parent / "tests").glob("test_*.py")
)


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
