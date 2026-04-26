"""Tests for xsensai.xask.log — JSONL append + privacy modes + purge."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from xsensai.xask import log as xlog


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    """Point XDG_CACHE_HOME at tmp_path so the log lands in an isolated dir."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path / "xsensai" / "xask-log.jsonl"


def _common_kwargs():
    return {
        "top3": ["card-a", "card-b"],
        "candidates": 5,
        "web": "ok",
        "challenge_used": False,
        "challenge_status": None,
        "output_sha256": "abc1234567890def",
        "prompt_template_version": "1.0.0",
        "service_version": "1.0.0",
        "duration_ms": 1234,
    }


def test_append_writes_well_formed_json(isolated_log, monkeypatch):
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "full")
    path = xlog.append_log(question="What is leverage?", **_common_kwargs())
    assert path == isolated_log
    assert isolated_log.exists()
    line = isolated_log.read_text().strip()
    parsed = json.loads(line)
    assert parsed["question"] == "What is leverage?"
    assert parsed["q_hash"]
    assert parsed["top3"] == ["card-a", "card-b"]
    assert parsed["prompt_template_version"] == "1.0.0"


def test_default_mode_is_hash_only(isolated_log, monkeypatch):
    """DX4 privacy: default mode strips question text."""
    monkeypatch.delenv("XSENSAI_XASK_LOG_MODE", raising=False)
    xlog.append_log(question="Confidential project context here", **_common_kwargs())
    parsed = json.loads(isolated_log.read_text().strip())
    assert parsed["question"] is None
    assert parsed["q_hash"]  # hash still present for repetition analysis


def test_off_mode_writes_nothing(isolated_log, monkeypatch):
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "off")
    result = xlog.append_log(question="anything", **_common_kwargs())
    assert result is None
    assert not isolated_log.exists()


def test_unicode_and_control_chars(isolated_log, monkeypatch):
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "full")
    weird = 'with "quotes" and\nnewlines and \x00 nulls and 中文 unicode'
    xlog.append_log(question=weird, **_common_kwargs())
    line = isolated_log.read_text().strip()
    parsed = json.loads(line)
    assert parsed["question"] == weird


def test_log_file_is_chmod_600(isolated_log, monkeypatch):
    """EC11 security: log captures plaintext (in full mode); restrict perms."""
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "full")
    xlog.append_log(question="x", **_common_kwargs())
    mode = isolated_log.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_log_dir_is_chmod_700(isolated_log, monkeypatch):
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "full")
    xlog.append_log(question="x", **_common_kwargs())
    mode = isolated_log.parent.stat().st_mode & 0o777
    assert mode == 0o700, f"expected 0o700, got {oct(mode)}"


def test_concurrent_appends_via_subprocess(isolated_log, monkeypatch, tmp_path):
    """fcntl.flock lets two processes append cleanly without truncation."""
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "full")
    helper = tmp_path / "helper.py"
    helper.write_text(
        "import os, sys\n"
        f"os.environ['XDG_CACHE_HOME'] = {str(tmp_path)!r}\n"
        "os.environ['XSENSAI_XASK_LOG_MODE'] = 'full'\n"
        "from xsensai.xask import log\n"
        "for i in range(50):\n"
        "    log.append_log(question=f'q{sys.argv[1]}-{i}',\n"
        "                   top3=[], candidates=0, web='ok',\n"
        "                   challenge_used=False, challenge_status=None,\n"
        "                   output_sha256='x'*16,\n"
        "                   prompt_template_version='1.0.0',\n"
        "                   service_version='1.0.0',\n"
        "                   duration_ms=1)\n"
    )
    procs = [
        subprocess.Popen([sys.executable, str(helper), str(i)])
        for i in range(3)
    ]
    for p in procs:
        assert p.wait() == 0
    lines = isolated_log.read_text().strip().split("\n")
    assert len(lines) == 150  # 3 procs × 50 each, no losses
    # Every line parses
    for line in lines:
        json.loads(line)


def test_purge_removes_old_entries(isolated_log, monkeypatch):
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "full")
    # Inject one fresh + one old entry directly
    isolated_log.parent.mkdir(parents=True, exist_ok=True)
    fresh_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat().replace(
        "+00:00", "Z"
    )
    with open(isolated_log, "w") as f:
        f.write(json.dumps({"ts": fresh_ts, "question": "fresh"}) + "\n")
        f.write(json.dumps({"ts": old_ts, "question": "old"}) + "\n")
    purged = xlog.purge(retention_days=90)
    assert purged == 1
    survivors = [json.loads(l) for l in isolated_log.read_text().strip().split("\n")]
    assert len(survivors) == 1
    assert survivors[0]["question"] == "fresh"


def test_purge_no_op_when_log_missing(isolated_log):
    assert not isolated_log.exists()
    assert xlog.purge() == 0


def test_purge_keeps_entries_just_inside_cutoff_boundary(isolated_log):
    """T10 fix: entry just inside the cutoff window is kept (>= compare).

    Uses a 1-second buffer to absorb the purge/test timing skew (purge calls
    datetime.now() at runtime; the test's `now` will be older). The semantic
    pin is: an entry at the boundary edge survives.
    """
    isolated_log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = datetime.now(timezone.utc)
    # 1 second INSIDE the 90-day window — tolerates the test↔purge time skew
    inside_iso = (
        now - timedelta(days=90) + timedelta(seconds=1)
    ).isoformat().replace("+00:00", "Z")
    isolated_log.write_text(
        json.dumps({"ts": inside_iso, "question": "just-inside"}) + "\n"
    )
    n = xlog.purge(retention_days=90)
    assert n == 0
    assert "just-inside" in isolated_log.read_text()


def test_purge_keeps_entries_with_missing_or_invalid_ts(isolated_log):
    """T10 fix: malformed/missing ts → keep out of caution (don't drop data)."""
    isolated_log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    isolated_log.write_text(
        json.dumps({"ts": "not-a-date", "question": "bad-ts"})
        + "\n"
        + json.dumps({"question": "no-ts-key"})
        + "\n"
    )
    n = xlog.purge(retention_days=90)
    assert n == 0
    body = isolated_log.read_text()
    assert "bad-ts" in body
    assert "no-ts-key" in body


# ----- F3: secret scrubbing -------------------------------------------------


def test_secret_scrubber_strips_common_patterns():
    """F3 fix: common credential patterns are redacted before logging."""
    samples = [
        ("sk-ant-api03-XYZabc123_DEFghi456jklmnoPQRStuv78901234", "[REDACTED:secret]"),
        ("sk-1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ", "[REDACTED:secret]"),
        ("ghp_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ", "[REDACTED:secret]"),
        ("AKIAIOSFODNN7EXAMPLE", "[REDACTED:secret]"),
        ("AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456", "[REDACTED:secret]"),
    ]
    for raw, marker in samples:
        scrubbed = xlog._scrub_secrets(f"why doesn't {raw} work?")
        assert raw not in scrubbed, f"secret {raw[:10]}... not scrubbed"
        assert marker in scrubbed


def test_secret_scrubber_preserves_normal_text():
    """Plain text without credentials is unchanged."""
    text = "what does Naval say about leverage and capital allocation?"
    assert xlog._scrub_secrets(text) == text


def test_full_mode_log_scrubs_secrets(isolated_log, monkeypatch):
    """End-to-end: full-mode logging redacts secrets in the persisted question."""
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "full")
    secret = "sk-ant-api03-VeryLongRealLookingKeyThatShouldBeRedacted123"
    xlog.append_log(question=f"is {secret} expired?", **_common_kwargs())
    parsed = json.loads(isolated_log.read_text().strip())
    assert secret not in parsed["question"]
    assert "[REDACTED:" in parsed["question"]


# ----- F5: started/completed log states -------------------------------------


def test_log_entry_state_field_defaults_to_completed(isolated_log, monkeypatch):
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "full")
    xlog.append_log(question="x", **_common_kwargs())
    parsed = json.loads(isolated_log.read_text().strip())
    assert parsed["state"] == "completed"


def test_log_entry_state_started(isolated_log, monkeypatch):
    """F5: state='started' is honored for the pre-synthesis sentinel."""
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "full")
    xlog.append_log(question="x", state="started", **_common_kwargs())
    parsed = json.loads(isolated_log.read_text().strip())
    assert parsed["state"] == "started"


# ----- F2: purge holds flock against concurrent append ----------------------


def test_purge_acquires_flock_on_live_log(isolated_log, monkeypatch):
    """Sanity: purge calls fcntl.flock at least once (the LOCK_EX hold).

    A full race-test would need two coordinated subprocesses. This sanity
    check pins that purge actually invokes flock — if the flock call is ever
    deleted, this test catches it.
    """
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "full")
    xlog.append_log(question="entry to keep", **_common_kwargs())

    flock_calls = []
    real_flock = xlog.fcntl.flock

    def _spy_flock(fd, op):
        flock_calls.append(op)
        return real_flock(fd, op)

    monkeypatch.setattr(xlog.fcntl, "flock", _spy_flock)
    xlog.purge(retention_days=90)
    assert any(op == xlog.fcntl.LOCK_EX for op in flock_calls), (
        "F2 fix regressed — purge() did not acquire fcntl.LOCK_EX"
    )


def test_iter_entries_yields_parsed_lines(isolated_log, monkeypatch):
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "full")
    xlog.append_log(question="a", **_common_kwargs())
    xlog.append_log(question="b", **_common_kwargs())
    entries = list(xlog.iter_entries())
    assert len(entries) == 2
    assert {e["question"] for e in entries} == {"a", "b"}
