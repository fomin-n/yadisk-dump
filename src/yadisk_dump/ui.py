"""All Rich and plain-terminal rendering for yadisk-dump."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import webbrowser
from collections import deque
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from yadisk_dump.api import DiskAccount
from yadisk_dump.config import POLYGON_URL
from yadisk_dump.downloader import DownloadOutcome, ProgressCallbacks, RunStats
from yadisk_dump.scanner import ScanSummary
from yadisk_dump.state import FileRecord

ACCENT = "#FFCC00"
TOKEN_INSTRUCTIONS = """
  No OAuth token found. Let's get one (takes ~30 seconds):

  1. Press Enter to open  [link={url}]{url}[/link]
  2. Click «Получить OAuth-токен» (top right), copy the token
  3. Paste it below (input is hidden)
"""


def make_console() -> Console:
    """Create the primary console while honoring ``NO_COLOR``."""
    return Console(
        no_color="NO_COLOR" in os.environ,
        highlight=False,
    )


class TerminalUI:
    """Render wizard, summaries, diagnostics, and completion output."""

    def __init__(self, *, quiet: bool = False, console: Console | None = None) -> None:
        """Initialize output behavior for the current terminal."""
        self.quiet = quiet
        self.console = console or make_console()
        self.input_interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())

    @property
    def live_enabled(self) -> bool:
        """Return whether animated output is suitable for this stream."""
        return not self.quiet and self.console.is_terminal

    def banner(self) -> None:
        """Render the compact product header."""
        if self.quiet:
            return
        body = Text()
        body.append(" yadisk-dump\n", style=f"bold {ACCENT}")
        body.append(" Download your entire Yandex.Disk. Read-only. Local.", style="dim")
        self.console.print(Panel(body, box=box.ROUNDED, border_style=ACCENT))
        self.console.print()

    def acquire_token(self) -> str:
        """Explain the manual OAuth flow, open the browser on Enter, and prompt secretly."""
        if not self.input_interactive:
            raise RuntimeError("interactive input is unavailable")
        if not self.quiet:
            self.console.print(TOKEN_INSTRUCTIONS.format(url=POLYGON_URL).rstrip())
        self.console.input("\n  Press Enter to open the page…")
        webbrowser.open(POLYGON_URL)
        return self.prompt_token()

    def prompt_token(self) -> str:
        """Prompt for a hidden credential without opening another browser tab."""
        return Prompt.ask("\n  Token", password=True, console=self.console).strip()

    def invalid_token(self) -> None:
        """Report a rejected credential without displaying any part of it."""
        self.error("Credential invalid — try pasting it again.")

    def account_valid(self, account: DiskAccount) -> None:
        """Show the validated account and quota."""
        if self.quiet:
            return
        self.console.print(
            "  [green]✓[/green] Credential valid — user "
            f"[bold]{account.login}[/bold], {human_size(account.used_space)} used of "
            f"{human_size(account.total_space)}"
        )

    def credential_saved(self, path: Path) -> None:
        """Report secure local credential storage."""
        if self.quiet:
            return
        suffix = "  (chmod 600)" if os.name != "nt" else ""
        self.console.print(f"  [green]✓[/green] Saved to {path}{suffix}")
        self.console.print()

    def ask_destination(self, default: Path) -> Path:
        """Prompt for the interactive flow's destination directory."""
        value = Prompt.ask(
            "  Where should files go?",
            default=display_path(default),
            console=self.console,
        )
        return Path(value).expanduser()

    def scanning(self) -> ScanDisplay:
        """Create a context manager and callback for scan progress."""
        return ScanDisplay(self)

    def summary(
        self,
        summary: ScanSummary,
        destination: Path,
        free_bytes: int,
        *,
        show_when_quiet: bool = False,
    ) -> None:
        """Render categorized scan totals and destination free space."""
        if self.quiet and not show_when_quiet:
            return
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold", width=12)
        table.add_column(justify="right")
        table.add_column(justify="right")
        for name in ("Photos", "Videos", "Documents", "Other"):
            total = summary.categories[name]
            table.add_row(name, f"{total.files:,} files", human_size(total.bytes))
        table.add_row("", "", "")
        table.add_row(
            "Total",
            f"{summary.total_files:,} files",
            human_size(summary.total_bytes),
            style="bold",
        )
        table.add_row("", "", "")
        table.add_row(
            "Destination",
            display_path(destination),
            f"free: {human_size(free_bytes)}",
        )
        self.console.print(
            Panel(
                table,
                title="Found on disk",
                title_align="left",
                box=box.ROUNDED,
                border_style=ACCENT,
            )
        )

    def confirm_download(self) -> bool:
        """Ask for the default-yes interactive download confirmation."""
        if not self.input_interactive:
            raise RuntimeError("confirmation requires an interactive terminal; use --yes")
        return Confirm.ask("  Start download?", default=True, console=self.console)

    def download_progress(self, total_bytes: int, total_files: int) -> DownloadDisplay:
        """Create callbacks for live or plain download output."""
        return DownloadDisplay(self, total_bytes, total_files)

    def completion(self, stats: RunStats) -> None:
        """Render the final summary and repair commands."""
        mark = "[green]✓[/green]" if stats.failed == 0 else "[red]✗[/red]"
        self.console.print()
        self.console.print(f"  {mark} Done in {human_duration(stats.elapsed)}")
        self.console.print(
            f"    {stats.downloaded:,} downloaded · {stats.skipped:,} skipped "
            f"(already present) · {stats.failed:,} failed"
        )
        if stats.failed:
            self.console.print("\n  Retry failures:   yadisk-dump retry")
        self.console.print("  Verify checksums: yadisk-dump verify")

    def status(self, counts: dict[str, int], last_run: str | None, last_scan: str | None) -> None:
        """Render persisted counters and last-run metadata."""
        table = Table(box=box.ROUNDED, border_style=ACCENT, show_header=False)
        table.add_column(style="bold")
        table.add_column(justify="right")
        for name in ("pending", "done", "skipped", "failed"):
            table.add_row(name.capitalize(), f"{counts[name]:,}")
        table.add_row("Last run", last_run or "never")
        table.add_row("Last scan", last_scan or "never")
        self.console.print(table)

    def verify_result(self, checked: int, mismatched: int, unavailable: int) -> None:
        """Render checksum verification totals."""
        style = "green" if mismatched == 0 else "red"
        self.console.print(
            f"  [{style}]{'✓' if mismatched == 0 else '✗'}[/{style}] "
            f"{checked:,} checked · {mismatched:,} mismatched or missing · "
            f"{unavailable:,} without remote MD5"
        )
        if mismatched:
            self.console.print("  Run yadisk-dump retry to repair failed files.")

    def verify_file(self, record: FileRecord, result: str) -> None:
        """Print non-success verification results without noisy per-file success lines."""
        if result in {"mismatch", "missing"}:
            self.console.print(f"  [red]✗[/red] {record.local_path} — {result}")

    def interrupted(self) -> None:
        """Render the cooperative interruption message."""
        self.console.print("\n  Interrupted — run again to resume.")

    def error(self, message: str) -> None:
        """Render a safe operational error."""
        self.console.print(f"  [red]✗[/red] {message}")

    def warning(self, message: str) -> None:
        """Render a warning unless quiet mode suppresses it."""
        if not self.quiet:
            self.console.print(f"  [red]Warning:[/red] {message}")

    def info(self, message: str) -> None:
        """Render informational output unless quiet mode suppresses it."""
        if not self.quiet:
            self.console.print(f"  {message}")


class ScanDisplay(AbstractContextManager["ScanDisplay"]):
    """Update a Rich status line while the scanner discovers files."""

    def __init__(self, ui: TerminalUI) -> None:
        self.ui = ui
        self._status: Any = None

    def __enter__(self) -> ScanDisplay:
        if self.ui.live_enabled:
            self._status = self.ui.console.status("  Scanning disk…", spinner="dots")
            self._status.start()
        elif not self.ui.quiet:
            self.ui.console.print("  Scanning disk…")
        return self

    def __exit__(self, *_args: object) -> None:
        if self._status is not None:
            self._status.stop()

    def update(self, summary: ScanSummary) -> None:
        """Refresh the current discovery totals."""
        if self._status is not None:
            self._status.update(
                f"  Scanning disk…  {summary.total_files:,} files · "
                f"{human_size(summary.total_bytes)} found"
            )


class DownloadDisplay(ProgressCallbacks, AbstractContextManager["DownloadDisplay"]):
    """Thread-safe live progress with a non-TTY per-file fallback."""

    def __init__(self, ui: TerminalUI, total_bytes: int, total_files: int) -> None:
        self.ui = ui
        self.total_bytes = total_bytes
        self.total_files = total_files
        self._lock = threading.RLock()
        self._tasks: dict[str, TaskID] = {}
        self._overall_completed = 0
        self._network_bytes = 0
        self._samples: deque[tuple[float, int]] = deque()
        self._stats = RunStats(total_files=total_files, total_bytes=total_bytes)
        self._files = Progress(
            SpinnerColumn(style=ACCENT),
            TextColumn("{task.description}", justify="left"),
            BarColumn(bar_width=None, style="dim", complete_style=ACCENT),
            DownloadColumn(),
            console=ui.console,
            expand=True,
        )
        self._overall = Progress(
            TextColumn("[bold]Overall"),
            BarColumn(bar_width=None, style="dim", complete_style=ACCENT),
            DownloadColumn(),
            console=ui.console,
            expand=True,
        )
        self._overall_task = self._overall.add_task("Overall", total=max(total_bytes, 1))
        self._live: Live | None = None

    def __enter__(self) -> DownloadDisplay:
        if self.ui.live_enabled:
            self._live = Live(
                self._render_group(),
                console=self.ui.console,
                refresh_per_second=8,
                transient=False,
            )
            self._live.start()
        return self

    def __exit__(self, *_args: object) -> None:
        if self._live is not None:
            self._live.stop()

    def file_started(self, record: FileRecord) -> None:
        """Add one bounded in-flight task."""
        if not self.ui.live_enabled:
            return
        with self._lock:
            description = middle_truncate(record.local_path, 44)
            self._tasks[record.remote_path] = self._files.add_task(
                description,
                total=max(record.size, 1),
            )
            self._refresh()

    def chunk(self, record: FileRecord, delta: int) -> None:
        """Advance or roll back file and overall byte progress."""
        if not self.ui.live_enabled:
            return
        with self._lock:
            task = self._tasks.get(record.remote_path)
            if task is not None:
                self._files.advance(task, delta)
            self._overall_completed = max(0, self._overall_completed + delta)
            self._overall.update(self._overall_task, completed=self._overall_completed)
            if delta > 0:
                self._network_bytes += delta
                now = time.monotonic()
                self._samples.append((now, self._network_bytes))
                while self._samples and now - self._samples[0][0] > 10:
                    self._samples.popleft()
            self._refresh()

    def file_retry(self, record: FileRecord, attempt: int, rolled_back: int) -> None:
        """Reset an in-flight task's visible byte count for a retry."""
        if not self.ui.live_enabled:
            return
        with self._lock:
            task = self._tasks.get(record.remote_path)
            if task is not None:
                self._files.update(task, completed=0)
            self._refresh()

    def file_finished(self, outcome: DownloadOutcome) -> None:
        """Remove a live task or emit one plain completion line."""
        if self.ui.live_enabled:
            with self._lock:
                task = self._tasks.pop(outcome.record.remote_path, None)
                if task is not None:
                    self._files.remove_task(task)
                if outcome.status == "skipped":
                    self._overall_completed += outcome.size
                    self._overall.update(
                        self._overall_task,
                        completed=self._overall_completed,
                    )
                self._refresh()
            return

        if self.ui.quiet and outcome.status != "failed":
            return
        if outcome.status == "done":
            self.ui.console.print(f"downloaded  {outcome.record.local_path}")
        elif outcome.status == "skipped":
            self.ui.console.print(f"skipped     {outcome.record.local_path}")
        else:
            self.ui.console.print(
                f"failed      {outcome.record.local_path or outcome.record.remote_path} — "
                f"{outcome.error or 'unknown error'}"
            )

    def totals(self, stats: RunStats) -> None:
        """Replace the aggregate counters used by the live status line."""
        if not self.ui.live_enabled:
            return
        with self._lock:
            self._stats = stats
            self._refresh()

    def _speed(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        elapsed = self._samples[-1][0] - self._samples[0][0]
        if elapsed <= 0:
            return 0.0
        return (self._samples[-1][1] - self._samples[0][1]) / elapsed

    def _stats_text(self) -> Text:
        speed = self._speed()
        remaining = max(0, self.total_bytes - self._overall_completed)
        eta = human_duration(remaining / speed) if speed > 0 else "—"
        completed_files = self._stats.downloaded + self._stats.skipped + self._stats.failed
        text = Text()
        text.append(
            f"  {completed_files:,}/{self.total_files:,} files · "
            f"{human_size(speed)}/s · ETA {eta} · ",
            style="dim",
        )
        text.append(f"✓ {self._stats.downloaded:,} downloaded", style="green")
        text.append(f" · ↷ {self._stats.skipped:,} skipped", style=ACCENT)
        text.append(f" · ✗ {self._stats.failed:,} failed", style="red")
        return text

    def _render_group(self) -> Group:
        return Group(self._files, Text(), self._overall, self._stats_text())

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render_group())


def display_path(path: Path) -> str:
    """Abbreviate the current home directory as ``~`` for display only."""
    expanded = path.expanduser().absolute()
    home = Path.home().absolute()
    try:
        relative = expanded.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if not relative.parts else f"~/{relative.as_posix()}"


def human_size(value: float) -> str:
    """Format a byte count with compact binary units."""
    amount = float(max(0, value))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} PB"


def human_duration(seconds: float) -> str:
    """Format seconds as a short duration suitable for progress output."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def middle_truncate(value: str, width: int) -> str:
    """Truncate a long filename in the middle while retaining both ends."""
    if len(value) <= width:
        return value
    left = max(1, (width - 1) // 2)
    right = max(1, width - left - 1)
    return f"{value[:left]}…{value[-right:]}"


def free_space(path: Path) -> int:
    """Return free bytes for the closest existing parent of a destination."""
    candidate = path.expanduser().absolute()
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return shutil.disk_usage(candidate).free
