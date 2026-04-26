"""Slice 4 — git cleanliness check + commit (UC-3=C + S-10 fix).

Per /autoplan: skip silently when vault is not a git repo. Surface
[INFO/VAULT_DIRTY_FIRST_RUN] when prior xsync output is uncommitted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from xsensai.sync.git_check import (
    VAULT_DIRTY_PROCEED_ENV,
    check_vault_state,
    commit_xsync_output,
    git_locked_envelope,
    should_proceed_dirty,
    vault_dirty_envelope,
    vault_not_git_envelope,
)


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo with a base commit so subsequent tests have a HEAD."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / ".gitignore").write_text("*.tmp\n")
    subprocess.run(["git", "-C", str(path), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def test_check_vault_state_returns_not_git_when_no_repo(tmp_path):
    state = check_vault_state(corpus_path=tmp_path)
    assert state.is_git_repo is False
    assert state.has_dirty_xsync_output is False


def test_check_vault_state_clean_when_git_repo_no_dirty_output(tmp_path):
    _init_git_repo(tmp_path)
    state = check_vault_state(corpus_path=tmp_path)
    assert state.is_git_repo is True
    assert state.has_dirty_xsync_output is False


def test_check_vault_state_detects_dirty_xsync_card(tmp_path):
    _init_git_repo(tmp_path)
    # Write an unstaged .md file (simulates a prior xsync that wasn't committed)
    (tmp_path / "2026-04-26-example-123.md").write_text("---\nsource_type: bookmark\n---\nhi\n")
    state = check_vault_state(corpus_path=tmp_path)
    assert state.has_dirty_xsync_output is True
    assert any("123.md" in str(p) for p in state.dirty_paths)


def test_check_vault_state_ignores_unrelated_files(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "random.txt").write_text("random")  # not .md or .raw.txt
    state = check_vault_state(corpus_path=tmp_path)
    assert state.has_dirty_xsync_output is False


def test_should_proceed_dirty_when_user_keyword(monkeypatch):
    monkeypatch.delenv(VAULT_DIRTY_PROCEED_ENV, raising=False)
    assert should_proceed_dirty(user_keyword=True) is True


def test_should_proceed_dirty_when_env_set(monkeypatch):
    monkeypatch.setenv(VAULT_DIRTY_PROCEED_ENV, "1")
    assert should_proceed_dirty(user_keyword=False) is True


def test_should_proceed_dirty_default_false(monkeypatch):
    monkeypatch.delenv(VAULT_DIRTY_PROCEED_ENV, raising=False)
    assert should_proceed_dirty(user_keyword=False) is False


def test_vault_dirty_envelope_lists_paths(tmp_path):
    state = check_vault_state(corpus_path=tmp_path)
    # Build a fake state with paths
    from xsensai.sync.git_check import GitState
    fake = GitState(
        is_git_repo=True,
        has_dirty_xsync_output=True,
        dirty_paths=[Path("a.md"), Path("b.md")],
    )
    env = vault_dirty_envelope(fake)
    rendered = env.format()
    assert "VAULT_DIRTY_FIRST_RUN" in rendered
    assert "a.md" in rendered
    assert "proceed dirty" in rendered.lower() or "PROCEED_DIRTY" in rendered.upper()


def test_vault_not_git_envelope():
    env = vault_not_git_envelope()
    assert "VAULT_NOT_GIT" in env.format()


def test_git_locked_envelope():
    env = git_locked_envelope()
    assert "GIT_LOCKED" in env.format()


def test_commit_xsync_output_no_op_when_not_git_repo(tmp_path):
    """If vault isn't a git repo, commit silently no-ops."""
    sha = commit_xsync_output(
        [tmp_path / "fake.md"], corpus_path=tmp_path,
        n_new_cards=1, extraction_pending_count=0,
    )
    assert sha is None


def test_commit_xsync_output_creates_commit(tmp_path):
    """Real git repo + real .md file → real commit with the standard message."""
    _init_git_repo(tmp_path)
    md_path = tmp_path / "2026-04-26-example-555.md"
    md_path.write_text("---\nsource_type: bookmark\n---\nhi\n")
    sha = commit_xsync_output(
        [md_path], corpus_path=tmp_path,
        n_new_cards=1, extraction_pending_count=0,
    )
    assert sha is not None
    assert sha != "unknown"
    # Verify the commit landed
    log = subprocess.run(
        ["git", "-C", str(tmp_path), "log", "--oneline", "-1"],
        capture_output=True, text=True, check=True,
    )
    assert "xsync: 1 new cards" in log.stdout


def test_commit_xsync_output_includes_pending_count_in_message(tmp_path):
    _init_git_repo(tmp_path)
    md_path = tmp_path / "2026-04-26-example-666.md"
    md_path.write_text("---\nsource_type: bookmark\n---\nhi\n")
    commit_xsync_output(
        [md_path], corpus_path=tmp_path,
        n_new_cards=3, extraction_pending_count=2,
    )
    log = subprocess.run(
        ["git", "-C", str(tmp_path), "log", "--oneline", "-1"],
        capture_output=True, text=True, check=True,
    )
    assert "extraction-pending" in log.stdout
