from __future__ import annotations

from pathlib import Path

from yadisk_dump.state import StateStore


def _scan_file(
    state: StateStore,
    remote_path: str = "disk:/a.txt",
    *,
    size: int = 10,
    md5: str = "one",
) -> None:
    state.upsert_file(
        remote_path,
        size=size,
        md5=md5,
        modified="2026-01-01T00:00:00Z",
        local_path=remote_path.removeprefix("disk:/"),
    )


def test_scan_preserves_state_and_forces_changed_remote(tmp_path: Path) -> None:
    with StateStore(tmp_path / "backup") as state:
        state.start_scan()
        _scan_file(state)
        state.finish_scan()
        state.set_status("disk:/a.txt", "done")

        state.start_scan()
        _scan_file(state)
        state.finish_scan()
        unchanged = state.get_file("disk:/a.txt")
        assert unchanged is not None
        assert unchanged.status == "done"
        assert not unchanged.force_download

        state.start_scan()
        _scan_file(state, md5="two")
        state.finish_scan()
        changed = state.get_file("disk:/a.txt")
        assert changed is not None
        assert changed.status == "pending"
        assert changed.force_download


def test_successful_scan_removes_inactive_rows_but_keeps_mappings(tmp_path: Path) -> None:
    with StateStore(tmp_path / "backup") as state:
        state.start_scan()
        _scan_file(state, "disk:/old.txt")
        state.save_path_mapping("disk:/old.txt", "file", "old.txt")
        state.finish_scan()

        state.start_scan()
        _scan_file(state, "disk:/new.txt")
        state.finish_scan()
        assert state.get_file("disk:/old.txt") is None
        assert state.get_file("disk:/new.txt") is not None
        assert ("disk:/old.txt", "file", "old.txt") in state.path_mappings()


def test_aborted_scan_rolls_back_partial_reconciliation(tmp_path: Path) -> None:
    with StateStore(tmp_path / "backup") as state:
        state.start_scan()
        _scan_file(state, "disk:/original.txt")
        state.finish_scan()
        state.start_scan()
        _scan_file(state, "disk:/partial.txt")
        state.abort_scan()
        assert state.get_file("disk:/original.txt") is not None
        assert state.get_file("disk:/partial.txt") is None


def test_status_transitions_counters_and_metadata(tmp_path: Path) -> None:
    with StateStore(tmp_path / "backup") as state:
        _scan_file(state, "disk:/a.txt")
        _scan_file(state, "disk:/b.txt")
        state.set_status("disk:/a.txt", "failed", error="safe reason")
        assert state.counters() == {"pending": 1, "done": 0, "skipped": 0, "failed": 1}
        assert state.reset_failed() == 1
        assert state.counters()["pending"] == 2
        state.set_meta("last_run_result", "complete")
        assert state.get_meta("last_run_result") == "complete"


def test_force_flag_can_be_set_for_verify_and_cleared_after_success(tmp_path: Path) -> None:
    with StateStore(tmp_path / "backup") as state:
        _scan_file(state)
        state.set_status("disk:/a.txt", "failed", force_download=True)
        assert state.get_file("disk:/a.txt").force_download  # type: ignore[union-attr]
        state.set_status("disk:/a.txt", "done", clear_force=True)
        assert not state.get_file("disk:/a.txt").force_download  # type: ignore[union-attr]

