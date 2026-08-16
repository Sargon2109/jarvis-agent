"""Enables ``python -m jarvis <command>`` — dispatches to the offline CLI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
