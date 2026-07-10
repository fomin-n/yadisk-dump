from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from yadisk_dump.scanner import DiskScanner
from yadisk_dump.state import StateStore


class FakeScanAPI:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.tree = {
            "disk:/": [
                SimpleNamespace(type="dir", path="disk:/Empty"),
                SimpleNamespace(type="dir", path="disk:/Photos"),
                SimpleNamespace(type="dir", path="disk:/Trash"),
                SimpleNamespace(
                    type="file",
                    path="disk:/notes.pdf",
                    size=4,
                    md5="abcd",
                    modified="now",
                ),
            ],
            "disk:/Empty": [],
            "disk:/Photos": [
                SimpleNamespace(
                    type="file",
                    path="disk:/Photos/image.HEIC",
                    size=6,
                    md5="efgh",
                    modified="now",
                )
            ],
        }

    def listdir(self, path: str) -> list[SimpleNamespace]:
        self.calls.append(path)
        return self.tree[path]


def test_scanner_walks_bfs_creates_empty_dirs_and_skips_trash(tmp_path: Path) -> None:
    destination = tmp_path / "backup"
    api = FakeScanAPI()
    updates: list[int] = []
    with StateStore(destination) as state:
        summary = DiskScanner(
            api,  # type: ignore[arg-type]
            state,
            destination,
            on_progress=lambda value: updates.append(value.total_files),
        ).scan()
        assert summary.total_files == 2
        assert summary.total_bytes == 10
        assert summary.categories["Photos"].files == 1
        assert summary.categories["Documents"].files == 1
        assert len(state.list_files()) == 2
    assert api.calls == ["disk:/", "disk:/Empty", "disk:/Photos"]
    assert (destination / "Empty").is_dir()
    assert updates[-1] == 2

