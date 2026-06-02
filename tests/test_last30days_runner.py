"""Tests for xsensai.web_fork.last30days_runner — env scrub + path validation
+ subprocess outcomes."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from xsensai.web_fork import last30days_runner as runner


# asyncio_mode = "auto" (pyproject) auto-marks async test functions, so no
# file-level `pytestmark = pytest.mark.asyncio` is needed. Adding one wrongly
# tagged the lone sync test below and emitted a PytestWarning every run.


def _write_fake_binary(path: Path, body: str, executable: bool = True) -> None:
    path.write_text("#!/usr/bin/env python3\n" + body)
    if executable:
        path.chmod(0o755)


async def test_skipped_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("XSENSAI_LAST30DAYS_PATH", str(tmp_path / "does-not-exist.py"))
    result = await runner.run_last30days("test question", timeout_s=2.0)
    assert result["status"] == "skipped"
    assert result["reason"] == "last30days_not_installed"


async def test_ok_payload_when_binary_returns_json(tmp_path, monkeypatch):
    fake = tmp_path / "fake_l30.py"
    _write_fake_binary(
        fake,
        'import sys, json; print(json.dumps({"results": [{"title": "x"}]}))',
    )
    monkeypatch.setenv("XSENSAI_LAST30DAYS_PATH", str(fake))
    result = await runner.run_last30days("q", timeout_s=5.0)
    assert result["status"] == "ok"
    assert result["payload"]["results"] == [{"title": "x"}]


async def test_empty_payload_classified_as_empty(tmp_path, monkeypatch):
    fake = tmp_path / "fake_empty.py"
    _write_fake_binary(fake, 'import json; print(json.dumps({"results": []}))')
    monkeypatch.setenv("XSENSAI_LAST30DAYS_PATH", str(fake))
    result = await runner.run_last30days("q", timeout_s=5.0)
    assert result["status"] == "empty"


async def test_timeout_returns_missed(tmp_path, monkeypatch):
    fake = tmp_path / "slow.py"
    _write_fake_binary(fake, "import time; time.sleep(10); print('{}')")
    monkeypatch.setenv("XSENSAI_LAST30DAYS_PATH", str(fake))
    result = await runner.run_last30days("q", timeout_s=0.5)
    assert result["status"] == "missed"
    assert result["reason"] == "timeout"


async def test_nonzero_exit_returns_failed(tmp_path, monkeypatch):
    fake = tmp_path / "crashy.py"
    _write_fake_binary(
        fake,
        "import sys; sys.stderr.write('boom\\n'); sys.exit(2)",
    )
    monkeypatch.setenv("XSENSAI_LAST30DAYS_PATH", str(fake))
    result = await runner.run_last30days("q", timeout_s=5.0)
    assert result["status"] == "failed"
    assert "boom" in result["reason"]


async def test_malformed_json_returns_failed(tmp_path, monkeypatch):
    fake = tmp_path / "garbage.py"
    _write_fake_binary(fake, "print('this is not json')")
    monkeypatch.setenv("XSENSAI_LAST30DAYS_PATH", str(fake))
    result = await runner.run_last30days("q", timeout_s=5.0)
    assert result["status"] == "failed"
    assert "parse_error" in result["reason"]


async def test_env_is_scrubbed(tmp_path, monkeypatch):
    """EC6: subprocess must NOT see ANTHROPIC_API_KEY or X_TOKEN."""
    fake = tmp_path / "env_dump.py"
    _write_fake_binary(
        fake,
        "import os, json; print(json.dumps({'env': sorted(os.environ.keys())}))",
    )
    monkeypatch.setenv("XSENSAI_LAST30DAYS_PATH", str(fake))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-DO-NOT-LEAK")
    monkeypatch.setenv("X_API_TOKEN", "x-secret-DO-NOT-LEAK")
    result = await runner.run_last30days("q", timeout_s=5.0)
    assert result["status"] == "ok"
    env_seen = result["payload"]["env"]
    assert "ANTHROPIC_API_KEY" not in env_seen, (
        "EC6 fix regressed — subprocess saw ANTHROPIC_API_KEY"
    )
    assert "X_API_TOKEN" not in env_seen, (
        "EC6 fix regressed — subprocess saw X_API_TOKEN"
    )
    # Allowed passthroughs are still there:
    assert "PATH" in env_seen
    assert "HOME" in env_seen


async def test_env_scrub_blocks_all_common_secret_vars(tmp_path, monkeypatch):
    """T4 fix: pin that ALL common cloud/CI secret-shaped vars are scrubbed,
    not just ANTHROPIC_API_KEY + X_API_TOKEN."""
    fake = tmp_path / "env.py"
    _write_fake_binary(
        fake,
        "import os, json; print(json.dumps({'env': sorted(os.environ.keys())}))",
    )
    monkeypatch.setenv("XSENSAI_LAST30DAYS_PATH", str(fake))
    secret_vars = (
        "OPENAI_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "NPM_TOKEN",
        "ANTHROPIC_API_KEY",
        "X_API_TOKEN",
        "GOOGLE_API_KEY",
    )
    for var in secret_vars:
        monkeypatch.setenv(var, f"secret-{var}")
    result = await runner.run_last30days("q", timeout_s=5.0)
    assert result["status"] == "ok"
    seen = set(result["payload"]["env"])
    leaked = seen & set(secret_vars)
    assert not leaked, f"env scrub leaked: {sorted(leaked)}"


async def test_skipped_when_binary_is_symlink(tmp_path, monkeypatch):
    """S2 fix: lstat-based symlink check refuses symlinks even if the target
    is owned by us. Mitigates the symlink-pivot uid-bypass."""
    real = tmp_path / "real_l30.py"
    _write_fake_binary(real, 'import json; print(json.dumps({"x":1}))')
    link = tmp_path / "linked_l30.py"
    link.symlink_to(real)
    monkeypatch.setenv("XSENSAI_LAST30DAYS_PATH", str(link))
    result = await runner.run_last30days("q", timeout_s=2.0)
    assert result == {
        "status": "skipped",
        "reason": "executable_is_symlink_refused",
    }


async def test_question_too_long_returns_failed(tmp_path, monkeypatch):
    """S4 fix: bound question length so a runaway prompt-injected card body
    can't blow ARG_MAX or balloon the question log."""
    fake = tmp_path / "fake.py"
    _write_fake_binary(fake, 'print("{}")')
    monkeypatch.setenv("XSENSAI_LAST30DAYS_PATH", str(fake))
    huge = "x" * (runner.MAX_QUESTION_CHARS + 1)
    result = await runner.run_last30days(huge, timeout_s=2.0)
    assert result["status"] == "failed"
    assert "question_too_long" in result["reason"]


def test_secret_name_allowlist_invariant():
    """S8 fix: pin that no secret-shaped name is in the allowlist. If a
    future maintainer adds AWS_SECRET_ACCESS_KEY, the regex catches it.
    """
    for name in runner._ALLOWED_PASSTHROUGH:
        assert not runner._SECRET_NAME_RE.search(name), (
            f"_ALLOWED_PASSTHROUGH leaked a secret-shaped name: {name}"
        )
