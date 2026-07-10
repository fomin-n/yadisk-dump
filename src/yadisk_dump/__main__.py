"""Module entry point for ``python -m yadisk_dump``."""

from __future__ import annotations

from yadisk_dump.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
