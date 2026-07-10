from __future__ import annotations

from yadisk_dump.ui import human_duration, human_size, make_console, middle_truncate


def test_human_formatters() -> None:
    assert human_size(0) == "0 B"
    assert human_size(1024) == "1.0 KB"
    assert human_duration(3725) == "1h 2m"


def test_middle_truncation_preserves_both_ends() -> None:
    value = middle_truncate("Photos/a-very-long-filename.jpg", 16)
    assert len(value) == 16
    assert value.startswith("Photos/")
    assert value.endswith("ame.jpg")


def test_no_color_environment_is_respected(monkeypatch: object) -> None:
    monkeypatch.setenv("NO_COLOR", "1")  # type: ignore[attr-defined]
    assert make_console().no_color
