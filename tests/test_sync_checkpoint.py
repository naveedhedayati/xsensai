"""Slice 4 — checkpoint append-on-success + crash-resume."""

from __future__ import annotations

from pathlib import Path

from xsensai.sync.checkpoint import CheckpointFile, CheckpointRecord


def _record(sid: str, run_id: str = "run-abc") -> CheckpointRecord:
    return CheckpointRecord(
        source_id=sid,
        captured_at="2026-04-26T12:00:00+00:00",
        mode="since-last-run",
        run_id=run_id,
    )


def test_append_writes_a_jsonl_line(tmp_path):
    cp = CheckpointFile(tmp_path)
    cp.append(_record("123"))
    raw = cp.path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert '"source_id": "123"' in raw


def test_existing_source_ids_round_trips(tmp_path):
    cp = CheckpointFile(tmp_path)
    cp.append(_record("100"))
    cp.append(_record("200"))
    cp.append(_record("300"))
    assert cp.existing_source_ids() == {"100", "200", "300"}


def test_partial_line_is_recovered_silently(tmp_path):
    """Crash mid-write leaves a trailing line without \\n; reader must skip."""
    cp = CheckpointFile(tmp_path)
    cp.append(_record("100"))
    # Simulate partial write by appending a line WITHOUT terminating newline.
    with open(cp.path, "ab") as f:
        f.write(b'{"source_id": "200", "captured_at": "2026-...')
    # Reader should skip the partial line and only see 100.
    assert cp.existing_source_ids() == {"100"}


def test_malformed_line_skipped_with_warning(tmp_path):
    cp = CheckpointFile(tmp_path)
    cp.append(_record("100"))
    with open(cp.path, "ab") as f:
        f.write(b"NOT VALID JSON\n")
    cp.append(_record("200"))
    assert cp.existing_source_ids() == {"100", "200"}


def test_archive_moves_file_to_user_cache(tmp_path, monkeypatch):
    """archive() moves the live checkpoint to ~/.cache/xsensai/sync-checkpoints/."""
    fake_cache = tmp_path / "fake-cache"
    monkeypatch.setattr(Path, "home", lambda: fake_cache)
    cp = CheckpointFile(tmp_path)
    cp.append(_record("100"))
    archive_path = cp.archive(run_id="abcdef12")
    assert archive_path is not None
    assert archive_path.exists()
    assert "abcdef12" in archive_path.name
    # Live file removed
    assert not cp.path.exists()


def test_archive_no_op_when_file_missing(tmp_path):
    """archive() returns None when there's nothing to archive."""
    cp = CheckpointFile(tmp_path)
    assert cp.archive(run_id="x") is None
