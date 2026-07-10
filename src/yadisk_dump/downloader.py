"""Concurrent streaming download engine with atomic local writes."""

from __future__ import annotations

import errno
import hashlib
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from yadisk_dump.api import (
    ApiError,
    OperationCancelled,
    RemoteMissingError,
    TokenExpiredError,
    TransientApiError,
    YandexDiskAPI,
    retry_after_seconds,
    validate_download_url,
)
from yadisk_dump.paths import PathSafetyError, resolve_local_path, to_io_path
from yadisk_dump.state import FileRecord, StateStore, utc_now

CHUNK_SIZE = 2 * 1024 * 1024
TRANSFER_TIMEOUT = (10.0, 120.0)
MAX_ATTEMPTS = 5
MAX_REDIRECTS = 3


class DownloadError(RuntimeError):
    """A safe, one-line failure reason suitable for state and UI output."""


class DiskFullError(DownloadError):
    """Raised when the destination volume has no free space."""


class DownloadInterrupted(DownloadError):
    """Raised after cooperative cancellation of a download run."""


@dataclass(slots=True)
class RunStats:
    """Counters for one invocation of the download engine."""

    total_files: int = 0
    total_bytes: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    completed_bytes: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def elapsed(self) -> float:
        """Return elapsed wall-clock seconds."""
        end = self.finished_at or time.monotonic()
        return max(0.0, end - self.started_at)


@dataclass(frozen=True, slots=True)
class DownloadOutcome:
    """Result returned by one worker."""

    record: FileRecord
    status: str
    size: int
    error: str | None = None


class ProgressCallbacks:
    """No-op callback surface implemented by Rich and plain-text UIs."""

    def file_started(self, record: FileRecord) -> None:
        """Handle the start of an in-flight file."""

    def chunk(self, record: FileRecord, delta: int) -> None:
        """Handle streamed or rolled-back bytes for an in-flight file."""

    def file_retry(self, record: FileRecord, attempt: int, rolled_back: int) -> None:
        """Reset a file display before a retry."""

    def file_finished(self, outcome: DownloadOutcome) -> None:
        """Handle a completed, skipped, or failed file."""

    def totals(self, stats: RunStats) -> None:
        """Refresh aggregate counters."""


class Downloader:
    """Download active state records through a bounded thread pool."""

    def __init__(
        self,
        api: YandexDiskAPI,
        state: StateStore,
        destination: Path,
        *,
        workers: int = 4,
        callbacks: ProgressCallbacks | None = None,
    ) -> None:
        """Initialize the engine with a worker count clamped to one through five."""
        self.api = api
        self.state = state
        self.destination = destination.expanduser().absolute()
        self.workers = min(5, max(1, workers))
        self.callbacks = callbacks or ProgressCallbacks()
        self._stats_lock = threading.Lock()

    def run(self, *, only_paths: set[str] | None = None) -> RunStats:
        """Download all active files, or only the selected remote paths."""
        records = self.state.list_files()
        if only_paths is not None:
            records = [record for record in records if record.remote_path in only_paths]
        stats = RunStats(
            total_files=len(records),
            total_bytes=sum(record.size for record in records),
            started_at=time.monotonic(),
        )
        self.state.set_meta("last_run_started", utc_now())
        self.state.set_meta("last_run_result", "running")
        self._remove_orphaned_parts()

        pending: list[FileRecord] = []
        for record in records:
            if not record.local_path:
                self.state.set_status(record.remote_path, "failed", error="unsafe local path")
                stats.failed += 1
                self.callbacks.file_finished(
                    DownloadOutcome(record, "failed", 0, "unsafe local path")
                )
                continue
            try:
                final_path = resolve_local_path(self.destination, record.local_path)
            except PathSafetyError:
                self.state.set_status(record.remote_path, "failed", error="unsafe local path")
                stats.failed += 1
                self.callbacks.file_finished(
                    DownloadOutcome(record, "failed", 0, "unsafe local path")
                )
                continue

            if (
                not record.force_download
                and to_io_path(final_path).is_file()
                and to_io_path(final_path).stat().st_size == record.size
            ):
                self.state.set_status(record.remote_path, "skipped", clear_force=True)
                stats.skipped += 1
                stats.completed_bytes += record.size
                outcome = DownloadOutcome(record, "skipped", record.size)
                self.callbacks.file_finished(outcome)
                self.callbacks.totals(stats)
            else:
                self.state.set_status(record.remote_path, "pending")
                pending.append(record)

        if pending:
            self._run_workers(pending, stats)

        stats.finished_at = time.monotonic()
        result = "failed" if stats.failed else "complete"
        self.state.set_meta("last_run_finished", utc_now())
        self.state.set_meta("last_run_result", result)
        self.callbacks.totals(stats)
        return stats

    def _run_workers(self, records: list[FileRecord], stats: RunStats) -> None:
        executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="yadisk")
        futures: dict[Future[DownloadOutcome], FileRecord] = {
            executor.submit(self._download_one, record): record for record in records
        }
        fatal: BaseException | None = None
        try:
            for future in as_completed(futures):
                try:
                    outcome = future.result()
                except (KeyboardInterrupt, DownloadInterrupted) as error:
                    fatal = error
                    self.api.stop_event.set()
                    break
                except (TokenExpiredError, DiskFullError) as error:
                    fatal = error
                    self.api.stop_event.set()
                    break
                except BaseException as error:
                    record = futures[future]
                    reason = _safe_failure_reason(error)
                    self.state.set_status(record.remote_path, "failed", error=reason)
                    outcome = DownloadOutcome(record, "failed", 0, reason)

                with self._stats_lock:
                    if outcome.status == "done":
                        stats.downloaded += 1
                        stats.completed_bytes += outcome.size
                    elif outcome.status == "failed":
                        stats.failed += 1
                self.callbacks.file_finished(outcome)
                self.callbacks.totals(stats)
        except KeyboardInterrupt as error:
            fatal = error
            self.api.stop_event.set()
        finally:
            if fatal is not None:
                for future in futures:
                    future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)

        if fatal is not None:
            self.state.set_meta("last_run_finished", utc_now())
            if isinstance(fatal, TokenExpiredError):
                self.state.set_meta("last_run_result", "token-expired")
                raise fatal
            if isinstance(fatal, DiskFullError):
                self.state.set_meta("last_run_result", "disk-full")
                raise fatal
            self.state.set_meta("last_run_result", "interrupted")
            raise DownloadInterrupted("Interrupted — run again to resume.") from fatal

    def _download_one(self, record: FileRecord) -> DownloadOutcome:
        self.callbacks.file_started(record)
        final_path = resolve_local_path(self.destination, record.local_path)
        part_path = Path(f"{final_path}.part")
        real_destination = Path(os.path.realpath(self.destination))
        resolve_local_path(
            self.destination,
            PureRelativePath.from_path(part_path.relative_to(real_destination)).value,
        )
        to_io_path(final_path.parent).mkdir(parents=True, exist_ok=True)

        last_reason = "download failed after retries"
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if self.api.stop_event.is_set():
                    raise DownloadInterrupted("Interrupted — run again to resume.")
                written = 0
                try:
                    link = self.api.get_download_link(record.remote_path, attempts=1)
                    allowed_host = validate_download_url(link)
                    response = self._open_response(link, allowed_host)
                    try:
                        if response.status_code == 404:
                            raise RemoteMissingError("remote file no longer exists")
                        if response.status_code in {401, 410}:
                            raise TransientApiError("temporary download link expired")
                        if response.status_code >= 500:
                            raise TransientApiError("Yandex download host is unavailable")
                        if response.status_code != 200:
                            raise DownloadError(
                                f"download host returned HTTP {response.status_code}"
                            )

                        expected_header = _content_length(response)
                        io_part = to_io_path(part_path)
                        try:
                            io_part.unlink()
                        except FileNotFoundError:
                            pass
                        with io_part.open("wb") as output:
                            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                                if self.api.stop_event.is_set():
                                    raise DownloadInterrupted(
                                        "Interrupted — run again to resume."
                                    )
                                if not chunk:
                                    continue
                                output.write(chunk)
                                written += len(chunk)
                                self.callbacks.chunk(record, len(chunk))
                            output.flush()
                            os.fsync(output.fileno())

                        if expected_header is not None and written != expected_header:
                            raise TransientApiError("Content-Length did not match downloaded bytes")
                        if written != record.size:
                            raise TransientApiError("downloaded size did not match remote metadata")
                        os.replace(to_io_path(part_path), to_io_path(final_path))
                        self.state.set_status(
                            record.remote_path,
                            "done",
                            clear_force=True,
                        )
                        return DownloadOutcome(record, "done", record.size)
                    finally:
                        response.close()
                except RemoteMissingError as error:
                    last_reason = str(error)
                    break
                except (DownloadInterrupted, TokenExpiredError, DiskFullError):
                    raise
                except OSError as error:
                    if error.errno == errno.ENOSPC:
                        self.state.set_status(
                            record.remote_path,
                            "failed",
                            error="destination disk is full",
                        )
                        raise DiskFullError(
                            "Destination disk is full; free space and run again to resume."
                        ) from error
                    last_reason = "local filesystem write failed"
                    if attempt >= MAX_ATTEMPTS:
                        break
                except (
                    requests.ConnectionError,
                    requests.Timeout,
                    TransientApiError,
                    OperationCancelled,
                ) as error:
                    if isinstance(error, OperationCancelled):
                        raise DownloadInterrupted(
                            "Interrupted — run again to resume."
                        ) from error
                    last_reason = _safe_failure_reason(error)
                    if attempt >= MAX_ATTEMPTS:
                        break
                except (ApiError, requests.RequestException, DownloadError) as error:
                    last_reason = _safe_failure_reason(error)
                    break

                if written:
                    self.callbacks.chunk(record, -written)
                self.callbacks.file_retry(record, attempt + 1, written)
                try:
                    to_io_path(part_path).unlink()
                except FileNotFoundError:
                    pass
                self.api.wait(min(2 ** (attempt - 1), 16))

            self.state.set_status(record.remote_path, "failed", error=last_reason)
            return DownloadOutcome(record, "failed", 0, last_reason)
        finally:
            try:
                to_io_path(part_path).unlink()
            except FileNotFoundError:
                pass

    def _open_response(self, link: str, allowed_host: str) -> requests.Response:
        session = self.api.transfer_session()
        current = link
        redirects = 0
        while True:
            response = session.get(
                current,
                stream=True,
                timeout=TRANSFER_TIMEOUT,
                allow_redirects=False,
            )
            if response.status_code == 429:
                delay = retry_after_seconds(response, default=60.0)
                response.close()
                self.api.wait(delay)
                continue
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("Location")
            response.close()
            if not location or redirects >= MAX_REDIRECTS:
                raise DownloadError("download host returned an unsafe redirect")
            current = urljoin(current, location)
            redirect_host = validate_download_url(current)
            if redirect_host != allowed_host or urlsplit(current).scheme != "https":
                raise DownloadError("download host returned a cross-host redirect")
            redirects += 1

    def _remove_orphaned_parts(self) -> None:
        if not self.destination.exists():
            return
        for part in self.destination.rglob("*.part"):
            try:
                relative = part.relative_to(self.destination).as_posix()
                safe = resolve_local_path(self.destination, relative)
                to_io_path(safe).unlink()
            except (FileNotFoundError, PathSafetyError, OSError):
                continue


@dataclass(frozen=True, slots=True)
class PureRelativePath:
    """Small adapter for converting an already-contained path to POSIX storage form."""

    value: str

    @classmethod
    def from_path(cls, path: Path) -> PureRelativePath:
        """Create the adapter from a relative platform path."""
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise PathSafetyError("unsafe local path")
        return cls("/".join(path.parts))


def verify_files(
    state: StateStore,
    destination: Path,
    on_result: Callable[[FileRecord, str], None] | None = None,
) -> tuple[int, int, int]:
    """Verify local MD5 values and mark missing or mismatched files as failed."""
    callback = on_result or (lambda _record, _result: None)
    checked = mismatched = unavailable = 0
    for record in state.list_files({"done", "skipped"}):
        if not record.md5:
            unavailable += 1
            callback(record, "no remote MD5")
            continue
        try:
            path = resolve_local_path(destination, record.local_path)
            digest = _md5_file(to_io_path(path))
        except (FileNotFoundError, PathSafetyError, OSError):
            mismatched += 1
            state.set_status(
                record.remote_path,
                "failed",
                error="local file is missing",
                force_download=True,
            )
            callback(record, "missing")
            continue
        checked += 1
        if digest.lower() != record.md5.lower():
            mismatched += 1
            state.set_status(
                record.remote_path,
                "failed",
                error="MD5 mismatch",
                force_download=True,
            )
            callback(record, "mismatch")
        else:
            callback(record, "ok")
    return checked, mismatched, unavailable


def _md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_length(response: requests.Response) -> int | None:
    value = response.headers.get("Content-Length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as error:
        raise TransientApiError("download host returned an invalid Content-Length") from error


def _safe_failure_reason(error: BaseException) -> str:
    if isinstance(error, TokenExpiredError):
        return "token expired — run yadisk-dump to re-authenticate"
    if isinstance(error, RemoteMissingError):
        return "remote file no longer exists"
    if isinstance(error, requests.Timeout):
        return "download timed out"
    if isinstance(error, requests.ConnectionError):
        return "download connection failed"
    if isinstance(error, OperationCancelled):
        return "download interrupted"
    if isinstance(error, (ApiError, DownloadError)):
        return str(error).splitlines()[0][:240]
    return "unexpected download failure"
