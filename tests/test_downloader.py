from __future__ import annotations

import errno
import hashlib
import threading
from collections.abc import Iterable
from pathlib import Path

import pytest

from yadisk_dump.downloader import (
    DiskFullError,
    Downloader,
    DownloadInterrupted,
    verify_files,
)
from yadisk_dump.state import StateStore

DATA = b"hello from Yandex"


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        *,
        data: bytes = DATA,
        headers: dict[str, str] | None = None,
        chunks: Iterable[bytes] | None = None,
    ) -> None:
        self.status_code = status
        self.data = data
        self.headers = headers or {"Content-Length": str(len(data))}
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterable[bytes]:
        del chunk_size
        return self._chunks if self._chunks is not None else [self.data]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class FakeAPI:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.stop_event = threading.Event()
        self.session = FakeSession(responses)
        self.links = 0
        self.waits: list[float] = []

    def get_download_link(self, path: str, *, attempts: int) -> str:
        del path, attempts
        self.links += 1
        return "https://downloader.disk.yandex.ru/signed-file"

    def transfer_session(self) -> FakeSession:
        return self.session

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        if self.stop_event.is_set():
            raise DownloadInterrupted("interrupted")


def _add_file(state: StateStore, *, data: bytes = DATA) -> None:
    state.upsert_file(
        "disk:/file.bin",
        size=len(data),
        md5=hashlib.md5(data).hexdigest(),
        modified="now",
        local_path="file.bin",
    )


def test_streams_to_part_then_atomically_finishes(tmp_path: Path) -> None:
    destination = tmp_path / "backup"
    api = FakeAPI([FakeResponse()])
    with StateStore(destination) as state:
        _add_file(state)
        stats = Downloader(api, state, destination).run()  # type: ignore[arg-type]
        assert stats.downloaded == 1
        assert (destination / "file.bin").read_bytes() == DATA
        assert not (destination / "file.bin.part").exists()
        assert state.get_file("disk:/file.bin").status == "done"  # type: ignore[union-attr]
    _url, kwargs = api.session.calls[0]
    assert kwargs["stream"] is True
    assert kwargs["allow_redirects"] is False
    assert "headers" not in kwargs


def test_existing_same_size_file_is_skipped_without_network(tmp_path: Path) -> None:
    destination = tmp_path / "backup"
    destination.mkdir()
    (destination / "file.bin").write_bytes(b"x" * len(DATA))
    api = FakeAPI([])
    with StateStore(destination) as state:
        _add_file(state)
        stats = Downloader(api, state, destination).run()  # type: ignore[arg-type]
        assert stats.skipped == 1
        assert api.links == 0
        assert state.get_file("disk:/file.bin").status == "skipped"  # type: ignore[union-attr]


def test_content_length_mismatch_retries_five_total_attempts(tmp_path: Path) -> None:
    responses = [
        FakeResponse(headers={"Content-Length": str(len(DATA) + 1)}) for _ in range(5)
    ]
    api = FakeAPI(responses)
    with StateStore(tmp_path / "backup") as state:
        _add_file(state)
        stats = Downloader(api, state, state.destination).run()  # type: ignore[arg-type]
        assert stats.failed == 1
        assert api.links == 5
        assert api.waits == [1, 2, 4, 8]
        assert state.get_file("disk:/file.bin").status == "failed"  # type: ignore[union-attr]


def test_expired_link_is_reacquired_immediately_before_retry(tmp_path: Path) -> None:
    api = FakeAPI([FakeResponse(status=410), FakeResponse()])
    with StateStore(tmp_path / "backup") as state:
        _add_file(state)
        stats = Downloader(api, state, state.destination).run()  # type: ignore[arg-type]
        assert stats.downloaded == 1
        assert api.links == 2


def test_cross_host_redirect_is_rejected_without_contacting_target(tmp_path: Path) -> None:
    response = FakeResponse(status=302, headers={"Location": "https://example.invalid/file"})
    api = FakeAPI([response])
    with StateStore(tmp_path / "backup") as state:
        _add_file(state)
        stats = Downloader(api, state, state.destination).run()  # type: ignore[arg-type]
        assert stats.failed == 1
        assert len(api.session.calls) == 1


def test_interruption_removes_part_and_keeps_pending_state(tmp_path: Path) -> None:
    api = FakeAPI([])

    def chunks() -> Iterable[bytes]:
        yield DATA[:4]
        api.stop_event.set()
        yield DATA[4:]

    api.session.responses.append(FakeResponse(chunks=chunks()))
    destination = tmp_path / "backup"
    with StateStore(destination) as state:
        _add_file(state)
        with pytest.raises(DownloadInterrupted):
            Downloader(api, state, destination).run()  # type: ignore[arg-type]
        assert not (destination / "file.bin.part").exists()
        assert state.get_file("disk:/file.bin").status == "pending"  # type: ignore[union-attr]


def test_disk_full_aborts_and_records_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeAPI([FakeResponse()])
    destination = tmp_path / "backup"
    original_open = Path.open

    def full_open(path: Path, *args: object, **kwargs: object):
        if str(path).endswith(".part"):
            raise OSError(errno.ENOSPC, "full")
        return original_open(path, *args, **kwargs)

    with StateStore(destination) as state:
        _add_file(state)
        monkeypatch.setattr(Path, "open", full_open)
        with pytest.raises(DiskFullError):
            Downloader(api, state, destination).run()  # type: ignore[arg-type]
        record = state.get_file("disk:/file.bin")
        assert record is not None
        assert record.status == "failed"
        assert record.error == "destination disk is full"


def test_verify_marks_mismatch_for_forced_retry(tmp_path: Path) -> None:
    destination = tmp_path / "backup"
    destination.mkdir()
    (destination / "file.bin").write_bytes(b"corrupt but same!!")
    with StateStore(destination) as state:
        _add_file(state)
        state.set_status("disk:/file.bin", "done")
        checked, mismatched, unavailable = verify_files(state, destination)
        assert (checked, mismatched, unavailable) == (1, 1, 0)
        record = state.get_file("disk:/file.bin")
        assert record is not None
        assert record.status == "failed"
        assert record.force_download
