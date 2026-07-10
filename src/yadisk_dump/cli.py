"""Argument parsing and command handlers for yadisk-dump."""

from __future__ import annotations

import argparse
import os
import time
from collections.abc import Sequence
from pathlib import Path

from yadisk_dump import __version__
from yadisk_dump.api import ApiError, TokenExpiredError, YandexDiskAPI
from yadisk_dump.config import (
    ENV_TOKEN,
    delete_token,
    load_download_dir,
    load_token,
    save_download_dir,
    save_token,
)
from yadisk_dump.downloader import (
    DiskFullError,
    Downloader,
    DownloadInterrupted,
    RunStats,
    verify_files,
)
from yadisk_dump.paths import PathSafetyError, resolve_local_path, to_io_path
from yadisk_dump.scanner import DiskScanner
from yadisk_dump.state import StateStore
from yadisk_dump.ui import TerminalUI, free_space

DEFAULT_DESTINATION = Path.home() / "YandexDisk"


class CLIError(RuntimeError):
    """A safe operational error that can be shown directly to a user."""


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser and all public subcommands."""
    parser = argparse.ArgumentParser(
        prog="yadisk-dump",
        description="Download your entire Yandex.Disk. Read-only. Local.",
    )
    _add_common_options(parser)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("pull", "scan and download without prompting"),
        ("scan", "scan and print the remote disk summary"),
        ("status", "show persisted counters and last-run information"),
        ("retry", "retry files currently marked failed"),
        ("verify", "verify local files against remote MD5 values"),
        ("logout", "delete the saved OAuth credential"),
    ):
        command_parser = subparsers.add_parser(name, help=help_text)
        _add_common_options(command_parser)
        command_parser.add_argument(
            "--version",
            action="version",
            version=f"%(prog)s {__version__}",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface and return a process exit code."""
    args = build_parser().parse_args(argv)
    quiet = bool(getattr(args, "quiet", False))
    ui = TerminalUI(quiet=quiet)
    try:
        return _dispatch(args, ui)
    except KeyboardInterrupt:
        ui.interrupted()
        return 130
    except DownloadInterrupted:
        ui.interrupted()
        return 130
    except (CLIError, ApiError, PathSafetyError, OSError) as error:
        ui.error(_safe_cli_message(error))
        return 1


def _dispatch(args: argparse.Namespace, ui: TerminalUI) -> int:
    command = args.command
    if command == "logout":
        return _logout(ui)

    destination = _destination_from_args(args)
    if command == "status":
        return _status(destination, ui)
    if command == "verify":
        return _verify(destination, ui)
    if command == "retry":
        return _retry(destination, int(getattr(args, "workers", 4)), ui)

    interactive = command is None
    if interactive:
        ui.banner()
    api, _newly_saved = _ensure_api(ui, interactive=interactive)

    if interactive:
        remembered = load_download_dir()
        explicit = getattr(args, "to", None)
        if explicit is None and remembered is None:
            if ui.input_interactive:
                destination = ui.ask_destination(DEFAULT_DESTINATION)
            elif bool(getattr(args, "yes", False)):
                destination = DEFAULT_DESTINATION
            else:
                raise CLIError(
                    "Interactive input is unavailable; use yadisk-dump pull --yes."
                )
        save_download_dir(destination)

    return _scan_and_maybe_pull(
        api,
        destination,
        workers=int(getattr(args, "workers", 4)),
        scan_only=command == "scan",
        interactive=interactive,
        assume_yes=bool(getattr(args, "yes", False)),
        ui=ui,
    )


def _scan_and_maybe_pull(
    api: YandexDiskAPI,
    destination: Path,
    *,
    workers: int,
    scan_only: bool,
    interactive: bool,
    assume_yes: bool,
    ui: TerminalUI,
) -> int:
    with StateStore(destination) as state:
        with ui.scanning() as display:
            summary = DiskScanner(
                api,
                state,
                destination,
                on_progress=display.update,
            ).scan()
        available = free_space(destination)
        ui.summary(
            summary,
            destination,
            available,
            show_when_quiet=scan_only,
        )
        required = _remaining_bytes(state, destination)
        if required > available:
            ui.warning(
                f"{required - available:,} more bytes are needed than the destination has free."
            )
        if scan_only:
            return 0
        if interactive and not assume_yes:
            if not ui.input_interactive:
                raise CLIError(
                    "Interactive input is unavailable; use yadisk-dump pull --yes."
                )
            if not ui.confirm_download():
                ui.info("Download cancelled; the completed scan is saved.")
                return 0

        callbacks = ui.download_progress(summary.total_bytes, summary.total_files)
        with callbacks:
            stats = Downloader(
                api,
                state,
                destination,
                workers=workers,
                callbacks=callbacks,
            ).run()
        ui.completion(stats)
        return 1 if stats.failed else 0


def _retry(destination: Path, workers: int, ui: TerminalUI) -> int:
    _require_state(destination)
    with StateStore(destination) as state:
        failed = set(state.failed_paths())
        if not failed:
            now = time.monotonic()
            ui.completion(RunStats(started_at=now, finished_at=now))
            return 0
        api, _new = _ensure_api(ui, interactive=False)
        records = [record for record in state.list_files() if record.remote_path in failed]
        state.reset_failed()
        callbacks = ui.download_progress(
            sum(record.size for record in records),
            len(records),
        )
        with callbacks:
            stats = Downloader(
                api,
                state,
                destination,
                workers=workers,
                callbacks=callbacks,
            ).run(only_paths=failed)
        ui.completion(stats)
        return 1 if stats.failed else 0


def _verify(destination: Path, ui: TerminalUI) -> int:
    _require_state(destination)
    with StateStore(destination) as state:
        checked, mismatched, unavailable = verify_files(
            state,
            destination,
            on_result=ui.verify_file,
        )
    ui.verify_result(checked, mismatched, unavailable)
    return 1 if mismatched else 0


def _status(destination: Path, ui: TerminalUI) -> int:
    _require_state(destination)
    with StateStore(destination) as state:
        result = state.get_meta("last_run_result")
        finished = state.get_meta("last_run_finished")
        last_run = " · ".join(value for value in (result, finished) if value) or None
        ui.status(state.counters(), last_run, state.get_meta("last_scan"))
    return 0


def _logout(ui: TerminalUI) -> int:
    deleted = delete_token()
    if deleted:
        ui.info("Saved credential deleted.")
    else:
        ui.info("No saved credential was found.")
    if os.environ.get(ENV_TOKEN):
        ui.warning(f"{ENV_TOKEN} is still set and continues to take precedence.")
    return 0


def _ensure_api(ui: TerminalUI, *, interactive: bool) -> tuple[YandexDiskAPI, bool]:
    token, source = load_token()
    if token:
        api = YandexDiskAPI(token)
        try:
            valid = api.check_token()
        except TokenExpiredError:
            valid = False
        if valid:
            api.get_disk_info()
            return api, False
        if source == "env":
            raise CLIError(
                f"{ENV_TOKEN} is invalid or expired; replace or unset it before retrying."
            )
        if not interactive:
            raise CLIError("Saved credential is invalid; run yadisk-dump to authenticate again.")
        ui.invalid_token()
    elif not interactive:
        raise CLIError(
            f"No OAuth credential found; set {ENV_TOKEN} or run yadisk-dump interactively."
        )

    if not ui.input_interactive:
        raise CLIError("Interactive input is unavailable; set YADISK_TOKEN and use pull --yes.")

    candidate = ui.acquire_token()
    while True:
        if candidate:
            api = YandexDiskAPI(candidate)
            try:
                valid = api.check_token()
            except TokenExpiredError:
                valid = False
            if valid:
                account = api.get_disk_info()
                ui.account_valid(account)
                saved_path = save_token(candidate)
                ui.credential_saved(saved_path)
                return api, True
        ui.invalid_token()
        candidate = ui.prompt_token()


def _destination_from_args(args: argparse.Namespace) -> Path:
    explicit = getattr(args, "to", None)
    if explicit is not None:
        return Path(explicit).expanduser().absolute()
    remembered = load_download_dir()
    return (remembered or DEFAULT_DESTINATION).expanduser().absolute()


def _remaining_bytes(state: StateStore, destination: Path) -> int:
    total = 0
    for record in state.list_files():
        if record.force_download or not record.local_path:
            total += record.size
            continue
        try:
            path = resolve_local_path(destination, record.local_path)
            if not to_io_path(path).is_file() or to_io_path(path).stat().st_size != record.size:
                total += record.size
        except (OSError, PathSafetyError):
            total += record.size
    return total


def _require_state(destination: Path) -> None:
    path = destination.expanduser().absolute() / ".yadisk-dump" / "state.db"
    if not path.is_file():
        raise CLIError(
            f"No backup state found in {destination}; run yadisk-dump pull --to DIR first."
        )


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--to",
        metavar="DIR",
        default=argparse.SUPPRESS,
        help="download directory (default: remembered path or ~/YandexDisk)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=argparse.SUPPRESS,
        help="concurrent downloads, clamped to 1-5 (default: 4)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=argparse.SUPPRESS,
        help="skip the interactive confirmation",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="show only errors and final summaries",
    )


def _safe_cli_message(error: BaseException) -> str:
    if isinstance(error, DiskFullError):
        return "Destination disk is full; free space and run again to resume."
    if isinstance(error, TokenExpiredError):
        return "Credential expired — run yadisk-dump to authenticate again."
    if isinstance(error, (CLIError, ApiError, PathSafetyError)):
        return str(error).splitlines()[0][:300]
    if isinstance(error, PermissionError):
        return "Permission denied while accessing the destination."
    return "Local filesystem operation failed."
