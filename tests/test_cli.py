from __future__ import annotations

from pathlib import Path

from yadisk_dump.cli import build_parser, main


def test_global_options_work_before_and_after_subcommand() -> None:
    before = build_parser().parse_args(["--to", "/tmp/a", "scan"])
    after = build_parser().parse_args(["scan", "--to", "/tmp/b"])
    assert before.to == "/tmp/a"
    assert after.to == "/tmp/b"


def test_workers_are_left_for_engine_clamping() -> None:
    assert build_parser().parse_args(["pull", "--workers", "99"]).workers == 99


def test_status_without_database_is_a_safe_error(tmp_path: Path, capsys: object) -> None:
    del capsys
    assert main(["status", "--to", str(tmp_path)]) == 1


def test_noninteractive_pull_without_credential_fails(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # type: ignore[attr-defined]
    monkeypatch.delenv("YADISK_TOKEN", raising=False)  # type: ignore[attr-defined]
    assert main(["pull", "--to", str(tmp_path / "backup"), "--quiet"]) == 1


def test_default_command_without_interactive_input_is_a_safe_error(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # type: ignore[attr-defined]
    monkeypatch.delenv("YADISK_TOKEN", raising=False)  # type: ignore[attr-defined]
    assert main(["--quiet"]) == 1
