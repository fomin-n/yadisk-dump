"""Command-line entry point."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from yadisk_dump import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(prog="yadisk-dump")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface and return a process exit code."""
    build_parser().parse_args(argv)
    return 0

