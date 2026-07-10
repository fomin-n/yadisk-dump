from __future__ import annotations

import os
from pathlib import Path

import pytest

from yadisk_dump.paths import (
    PathMapper,
    PathSafetyError,
    resolve_local_path,
    sanitize_component,
    split_remote_path,
    to_io_path,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('a<b>c:d"e|f?g*h\\i', "a_b_c_d_e_f_g_h_i"),
        ("name. ", "name"),
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("LPT9.log", "_LPT9.log"),
        ("normal имя 😀.jpg", "normal имя 😀.jpg"),
        ("control\x00name", "control_name"),
    ],
)
def test_sanitize_component(value: str, expected: str) -> None:
    assert sanitize_component(value) == expected


@pytest.mark.parametrize("value", ["", ".", ".."])
def test_sanitize_rejects_traversal_components(value: str) -> None:
    with pytest.raises(PathSafetyError):
        sanitize_component(value)


@pytest.mark.parametrize(
    "remote",
    ["disk:/../../etc/passwd", "disk:/a/../secret", "trash:/file", "/absolute"],
)
def test_split_remote_path_rejects_unsafe_namespaces(remote: str) -> None:
    with pytest.raises(PathSafetyError):
        split_remote_path(remote)


def test_resolve_local_path_stays_below_destination(tmp_path: Path) -> None:
    destination = tmp_path / "backup"
    destination.mkdir()
    assert resolve_local_path(destination, "Photos/a.jpg") == destination / "Photos/a.jpg"
    with pytest.raises(PathSafetyError):
        resolve_local_path(destination, "../outside")
    with pytest.raises(PathSafetyError):
        resolve_local_path(destination, "")
    with pytest.raises(PathSafetyError):
        resolve_local_path(destination, ".")


def test_resolve_local_path_rejects_symlink_escape(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("symlink creation is not generally available on Windows CI")
    destination = tmp_path / "backup"
    outside = tmp_path / "outside"
    destination.mkdir()
    outside.mkdir()
    (destination / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathSafetyError):
        resolve_local_path(destination, "link/file.txt")


def test_mapper_handles_case_and_sanitization_collisions_stably() -> None:
    saved: list[tuple[str, str, str]] = []
    mapper = PathMapper([], lambda *row: saved.append(row), case_insensitive=True)
    assert mapper.map("disk:/Foo.jpg", "file") == "Foo.jpg"
    assert mapper.map("disk:/foo.jpg", "file") == "foo_1.jpg"
    assert mapper.map("disk:/a:b.txt", "file") == "a_b.txt"
    assert mapper.map("disk:/a?b.txt", "file") == "a_b_1.txt"

    restored = PathMapper(saved, lambda *_row: None, case_insensitive=True)
    assert restored.map("disk:/foo.jpg", "file") == "foo_1.jpg"


def test_mapper_suffixes_colliding_directories_and_descendants() -> None:
    mapper = PathMapper([], lambda *_row: None, case_insensitive=True)
    assert mapper.map("disk:/Trips/A.jpg", "file") == "Trips/A.jpg"
    assert mapper.map("disk:/trips/B.jpg", "file") == "trips_1/B.jpg"


def test_windows_long_path_prefixes_local_and_unc_paths() -> None:
    local = Path("C:/") / ("a" * 250)
    unc = Path("//server/share") / ("b" * 250)
    assert str(to_io_path(local, platform="win32")).startswith("\\\\?\\")
    assert str(to_io_path(unc, platform="win32")).startswith("\\\\?\\UNC\\")
    assert to_io_path(Path("short"), platform="win32") == Path("short")
