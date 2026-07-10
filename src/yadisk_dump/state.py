"""Thread-safe SQLite state for scans, resume, and status reporting."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class FileRecord:
    """One remote file and its current local backup state."""

    remote_path: str
    size: int
    md5: str
    modified: str
    status: str
    local_path: str
    error: str | None
    updated_at: str
    force_download: bool = False


class StateStore:
    """Serialize access to the self-contained destination state database."""

    def __init__(self, destination: Path) -> None:
        """Open or create the state database below ``destination``."""
        self.destination = destination.expanduser().absolute()
        self.state_dir = self.destination / ".yadisk-dump"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "state.db"
        self._lock = threading.RLock()
        self._scan_active = False
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._create_schema()

    def __enter__(self) -> StateStore:
        """Return the open store."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Close the store."""
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            if self._scan_active:
                self._connection.rollback()
                self._scan_active = False
            self._connection.close()

    def start_scan(self) -> None:
        """Begin an atomic scan reconciliation transaction."""
        with self._lock:
            if self._scan_active:
                raise RuntimeError("a scan is already active")
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                "CREATE TEMP TABLE IF NOT EXISTS scan_seen "
                "(remote_path TEXT PRIMARY KEY)"
            )
            self._connection.execute("DELETE FROM scan_seen")
            self._scan_active = True

    def finish_scan(self) -> None:
        """Commit a completed scan and remove inactive remote rows from the view."""
        with self._lock:
            if not self._scan_active:
                raise RuntimeError("no scan is active")
            self._connection.execute(
                "DELETE FROM files WHERE remote_path NOT IN "
                "(SELECT remote_path FROM scan_seen)"
            )
            self._set_meta_unlocked("last_scan", utc_now())
            self._connection.commit()
            self._scan_active = False

    def abort_scan(self) -> None:
        """Roll back a partial scan so stale state is never treated as current."""
        with self._lock:
            if self._scan_active:
                self._connection.rollback()
                self._scan_active = False

    def upsert_file(
        self,
        remote_path: str,
        *,
        size: int,
        md5: str,
        modified: str,
        local_path: str,
    ) -> FileRecord:
        """Insert a file or refresh metadata while preserving valid state."""
        now = utc_now()
        with self._lock:
            previous = self._connection.execute(
                "SELECT * FROM files WHERE remote_path = ?", (remote_path,)
            ).fetchone()
            changed = previous is not None and (
                int(previous["size"]) != size or str(previous["md5"] or "") != md5
            )
            if previous is None:
                status, error, force_download = "pending", None, 0
            elif changed:
                status, error, force_download = "pending", None, 1
            else:
                status = str(previous["status"])
                error = previous["error"]
                force_download = int(previous["force_download"])

            self._connection.execute(
                """
                INSERT INTO files (
                    remote_path, size, md5, modified, status, local_path,
                    error, updated_at, force_download
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(remote_path) DO UPDATE SET
                    size=excluded.size,
                    md5=excluded.md5,
                    modified=excluded.modified,
                    status=excluded.status,
                    local_path=excluded.local_path,
                    error=excluded.error,
                    updated_at=excluded.updated_at,
                    force_download=excluded.force_download
                """,
                (
                    remote_path,
                    size,
                    md5,
                    modified,
                    status,
                    local_path,
                    error,
                    now,
                    force_download,
                ),
            )
            if self._scan_active:
                self._connection.execute(
                    "INSERT OR IGNORE INTO scan_seen(remote_path) VALUES (?)",
                    (remote_path,),
                )
            else:
                self._connection.commit()
            return FileRecord(
                remote_path,
                size,
                md5,
                modified,
                status,
                local_path,
                error,
                now,
                bool(force_download),
            )

    def set_status(
        self,
        remote_path: str,
        status: str,
        *,
        error: str | None = None,
        clear_force: bool = False,
        force_download: bool | None = None,
    ) -> None:
        """Set one file's state and optional safe one-line error reason."""
        if status not in {"pending", "done", "skipped", "failed"}:
            raise ValueError(f"invalid status: {status}")
        force_value = 0 if clear_force else (
            None if force_download is None else int(force_download)
        )
        with self._lock:
            self._connection.execute(
                "UPDATE files SET status=?, error=?, updated_at=?, "
                "force_download=COALESCE(?, force_download) "
                "WHERE remote_path=?",
                (status, error, utc_now(), force_value, remote_path),
            )
            self._commit_if_ready()

    def list_files(self, statuses: set[str] | None = None) -> list[FileRecord]:
        """Return active files, optionally filtered by state."""
        with self._lock:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = self._connection.execute(
                    f"SELECT * FROM files WHERE status IN ({placeholders}) "  # noqa: S608
                    "ORDER BY remote_path",
                    tuple(sorted(statuses)),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM files ORDER BY remote_path"
                ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get_file(self, remote_path: str) -> FileRecord | None:
        """Return one file record by remote path."""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM files WHERE remote_path=?", (remote_path,)
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def failed_paths(self) -> list[str]:
        """Return failed remote paths in deterministic order."""
        return [record.remote_path for record in self.list_files({"failed"})]

    def reset_failed(self) -> int:
        """Reset failed rows to pending and return the number changed."""
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE files SET status='pending', error=NULL, updated_at=? "
                "WHERE status='failed'",
                (utc_now(),),
            )
            self._commit_if_ready()
            return cursor.rowcount

    def counters(self) -> dict[str, int]:
        """Return file counts grouped by state, including zero-valued states."""
        counts = {name: 0 for name in ("pending", "done", "skipped", "failed")}
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM files GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[str(row["status"])] = int(row["count"])
        return counts

    def save_path_mapping(self, remote_path: str, kind: str, local_path: str) -> None:
        """Persist a stable remote-to-local path mapping."""
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO path_map(remote_path, kind, local_path) "
                "VALUES (?, ?, ?)",
                (remote_path, kind, local_path),
            )
            self._commit_if_ready()

    def path_mappings(self) -> list[tuple[str, str, str]]:
        """Return all persistent path mappings."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT remote_path, kind, local_path FROM path_map ORDER BY remote_path"
            ).fetchall()
        return [
            (str(row["remote_path"]), str(row["kind"]), str(row["local_path"]))
            for row in rows
        ]

    def set_meta(self, key: str, value: str) -> None:
        """Store a small metadata value such as the last run result."""
        with self._lock:
            self._set_meta_unlocked(key, value)
            self._commit_if_ready()

    def get_meta(self, key: str) -> str | None:
        """Load a metadata value."""
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return None if row is None else str(row["value"])

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                remote_path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                md5 TEXT NOT NULL,
                modified TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                local_path TEXT NOT NULL,
                error TEXT,
                updated_at TEXT NOT NULL,
                force_download INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_status ON files(status);
            CREATE TABLE IF NOT EXISTS path_map (
                remote_path TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                local_path TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1');
            """
        )
        self._connection.commit()

    def _set_meta_unlocked(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _commit_if_ready(self) -> None:
        if not self._scan_active:
            self._connection.commit()


def _row_to_record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        remote_path=str(row["remote_path"]),
        size=int(row["size"]),
        md5=str(row["md5"] or ""),
        modified=str(row["modified"] or ""),
        status=str(row["status"]),
        local_path=str(row["local_path"]),
        error=None if row["error"] is None else str(row["error"]),
        updated_at=str(row["updated_at"]),
        force_download=bool(row["force_download"]),
    )
