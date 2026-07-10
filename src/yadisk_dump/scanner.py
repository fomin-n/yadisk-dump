"""Breadth-first discovery of the complete remote disk tree."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from yadisk_dump.api import YandexDiskAPI
from yadisk_dump.paths import PathMapper, PathSafetyError, resolve_local_path, to_io_path
from yadisk_dump.state import StateStore

PHOTO_EXTENSIONS = {
    "jpg", "jpeg", "png", "heic", "heif", "webp", "gif", "bmp", "tiff",
    "raw", "arw", "cr2", "nef", "dng",
}
VIDEO_EXTENSIONS = {
    "mp4", "mov", "avi", "mkv", "m4v", "webm", "3gp", "mpg", "mpeg",
    "wmv", "mts", "m2ts",
}
DOCUMENT_EXTENSIONS = {
    "doc", "docx", "pdf", "txt", "rtf", "odt", "xls", "xlsx", "ods",
    "csv", "ppt", "pptx", "odp",
}
TRASH_PREFIXES = ("disk:/Корзина", "disk:/Trash")


@dataclass(slots=True)
class CategoryTotal:
    """File and byte totals for one summary category."""

    files: int = 0
    bytes: int = 0


@dataclass(slots=True)
class ScanSummary:
    """Categorized totals accumulated during a full scan."""

    categories: dict[str, CategoryTotal] = field(
        default_factory=lambda: {
            name: CategoryTotal()
            for name in ("Photos", "Videos", "Documents", "Other")
        }
    )

    @property
    def total_files(self) -> int:
        """Return the total number of discovered files."""
        return sum(value.files for value in self.categories.values())

    @property
    def total_bytes(self) -> int:
        """Return the total number of discovered bytes."""
        return sum(value.bytes for value in self.categories.values())

    def add(self, remote_path: str, size: int) -> None:
        """Add one file to its extension-based category."""
        extension = PurePosixPath(remote_path).suffix.lower().lstrip(".")
        if extension in PHOTO_EXTENSIONS:
            category = "Photos"
        elif extension in VIDEO_EXTENSIONS:
            category = "Videos"
        elif extension in DOCUMENT_EXTENSIONS:
            category = "Documents"
        else:
            category = "Other"
        self.categories[category].files += 1
        self.categories[category].bytes += max(0, size)


class DiskScanner:
    """Walk a remote Yandex.Disk iteratively and reconcile local state."""

    def __init__(
        self,
        api: YandexDiskAPI,
        state: StateStore,
        destination: Path,
        on_progress: Callable[[ScanSummary], None] | None = None,
    ) -> None:
        """Initialize a scanner and its stable path mapper."""
        self.api = api
        self.state = state
        self.destination = destination
        self.on_progress = on_progress or (lambda _summary: None)

    def scan(self) -> ScanSummary:
        """Scan ``disk:/`` completely and atomically reconcile the state database."""
        summary = ScanSummary()
        queue: deque[str] = deque(["disk:/"])
        self.state.start_scan()
        mapper = PathMapper(self.state.path_mappings(), self.state.save_path_mapping)
        try:
            while queue:
                directory = queue.popleft()
                items = sorted(
                    self.api.listdir(directory),
                    key=lambda item: str(getattr(item, "path", "")),
                )
                for item in items:
                    remote_path = str(getattr(item, "path", ""))
                    kind = str(getattr(item, "type", ""))
                    if _is_trash(remote_path):
                        continue
                    if kind == "dir":
                        try:
                            relative = mapper.map(remote_path, "dir")
                            local = resolve_local_path(self.destination, relative)
                            to_io_path(local).mkdir(parents=True, exist_ok=True)
                        except PathSafetyError:
                            continue
                        queue.append(remote_path)
                        continue
                    if kind != "file":
                        continue

                    size = max(0, int(getattr(item, "size", 0) or 0))
                    md5 = str(getattr(item, "md5", "") or "")
                    modified = str(getattr(item, "modified", "") or "")
                    summary.add(remote_path, size)
                    try:
                        relative = mapper.map(remote_path, "file")
                        resolve_local_path(self.destination, relative)
                    except PathSafetyError as error:
                        record = self.state.upsert_file(
                            remote_path,
                            size=size,
                            md5=md5,
                            modified=modified,
                            local_path="",
                        )
                        self.state.set_status(
                            record.remote_path,
                            "failed",
                            error=str(error),
                        )
                    else:
                        self.state.upsert_file(
                            remote_path,
                            size=size,
                            md5=md5,
                            modified=modified,
                            local_path=relative,
                        )
                    self.on_progress(summary)
            self.state.finish_scan()
        except BaseException:
            self.state.abort_scan()
            raise
        return summary


def _is_trash(remote_path: str) -> bool:
    return any(
        remote_path == prefix or remote_path.startswith(f"{prefix}/")
        for prefix in TRASH_PREFIXES
    )

