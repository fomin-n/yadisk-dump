"""Traversal-safe, cross-platform local path mapping."""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

_FORBIDDEN = re.compile(r'[<>:"|?*\\\x00-\x1f\x7f]')
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)


class PathSafetyError(ValueError):
    """Raised when a remote path cannot be mapped safely below the destination."""


def sanitize_component(component: str) -> str:
    """Sanitize one remote path component while preserving ordinary Unicode."""
    if component in {"", ".", ".."}:
        raise PathSafetyError("path traversal component rejected")
    sanitized = _FORBIDDEN.sub("_", component).rstrip(". ")
    if not sanitized:
        sanitized = "_"
    stem = sanitized.split(".", 1)[0]
    if _RESERVED.fullmatch(stem):
        sanitized = f"_{sanitized}"
    return sanitized


def split_remote_path(remote_path: str) -> list[str]:
    """Validate and split a ``disk:/`` remote path into components."""
    if remote_path == "disk:/":
        return []
    if not remote_path.startswith("disk:/"):
        raise PathSafetyError("remote path is outside the disk namespace")
    components = remote_path[6:].split("/")
    if not components or any(part in {"", ".", ".."} for part in components):
        raise PathSafetyError("path traversal component rejected")
    return components


def resolve_local_path(destination: Path, local_path: str | PurePosixPath) -> Path:
    """Resolve a stored relative path and prove that it stays below destination."""
    relative = PurePosixPath(local_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise PathSafetyError("unsafe local path")
    base = Path(os.path.realpath(destination.expanduser().absolute()))
    candidate = Path(os.path.realpath(base.joinpath(*relative.parts)))
    try:
        within = os.path.commonpath((str(base), str(candidate))) == str(base)
    except ValueError as error:
        raise PathSafetyError("local path is on a different volume") from error
    if not within:
        raise PathSafetyError("local path escapes the destination")
    return candidate


def to_io_path(path: Path, *, platform: str | None = None) -> Path:
    """Add a Windows extended-length prefix when necessary for filesystem I/O."""
    current = sys.platform if platform is None else platform
    value = str(path.absolute())
    if current != "win32":
        return path
    if sys.platform != "win32" and platform == "win32":
        value = str(path)
    windows_value = value.replace("/", "\\")
    if len(windows_value) <= 240 or windows_value.startswith("\\\\?\\"):
        return path
    if windows_value.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{windows_value[2:]}")
    return Path(f"\\\\?\\{windows_value}")


def is_case_insensitive_platform(platform: str | None = None) -> bool:
    """Return whether collision checks should ignore case for the platform."""
    current = sys.platform if platform is None else platform
    return current in {"darwin", "win32"}


class PathMapper:
    """Allocate stable, collision-free local paths for remote files and directories."""

    def __init__(
        self,
        mappings: Iterable[tuple[str, str, str]],
        save_mapping: Callable[[str, str, str], None],
        *,
        case_insensitive: bool | None = None,
    ) -> None:
        """Initialize the mapper from persisted ``(remote, kind, local)`` rows."""
        self._save_mapping = save_mapping
        self._case_insensitive = (
            is_case_insensitive_platform()
            if case_insensitive is None
            else case_insensitive
        )
        self._remote: dict[str, tuple[str, str]] = {}
        self._used: set[str] = set()
        for remote_path, kind, local_path in mappings:
            self._remote[remote_path] = (kind, local_path)
            self._used.add(self._collision_key(local_path))

    def map(self, remote_path: str, kind: str) -> str:
        """Return and persist a stable relative local path for a remote resource."""
        existing = self._remote.get(remote_path)
        if existing is not None:
            return existing[1]

        components = split_remote_path(remote_path)
        if not components:
            return "."

        parent_local = PurePosixPath()
        remote_parts: list[str] = []
        for index, raw_component in enumerate(components):
            remote_parts.append(raw_component)
            partial_remote = f"disk:/{'/'.join(remote_parts)}"
            mapped = self._remote.get(partial_remote)
            if mapped is not None:
                parent_local = PurePosixPath(mapped[1])
                continue

            partial_kind = kind if index == len(components) - 1 else "dir"
            component = sanitize_component(raw_component)
            candidate = parent_local / component
            suffix = 1
            while self._collision_key(candidate.as_posix()) in self._used:
                candidate = parent_local / _with_suffix(component, suffix, partial_kind)
                suffix += 1

            local_value = candidate.as_posix()
            self._remote[partial_remote] = (partial_kind, local_value)
            self._used.add(self._collision_key(local_value))
            self._save_mapping(partial_remote, partial_kind, local_value)
            parent_local = candidate
        return parent_local.as_posix()

    def _collision_key(self, local_path: str) -> str:
        normalized = unicodedata.normalize("NFC", local_path)
        return normalized.casefold() if self._case_insensitive else normalized


def _with_suffix(name: str, suffix: int, kind: str) -> str:
    if kind == "file":
        dot = name.rfind(".")
        if dot > 0:
            return f"{name[:dot]}_{suffix}{name[dot:]}"
    return f"{name}_{suffix}"
