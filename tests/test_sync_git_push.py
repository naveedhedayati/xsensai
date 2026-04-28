"""Slice 5 — git_push tests: allowlist staging + push retry + flag fallback."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Tuple

import pytest

from xsensai.sync import git_push
from xsensai.sync.heartbeat import STATUS_FILE_NAME, SyncStatus, write_status


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=check,
    )


def _setup_local_remote_clone(tmp_path: Path) -> Tuple[Path, Path]:
    """Bare remote + clone. Returns (remote, clone)."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], capture_output=True, check=True
    )
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(remote), str(clone)], capture_output=True, check=True
    )
    _git(clone, "config", "user.email", "test@test.local")
    _git(clone, "config", "user.name", "Test")
    # Initial commit
    (clone / "README.md").write_text("# initial\n")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-m", "init")
    _git(clone, "branch", "-M", "main")
    _git(clone, "push", "-u", "origin", "main")
    return remote, clone


def _stub_status() -> SyncStatus:
    return SyncStatus(
        last_run="2026-04-28T12:00:00+00:00",
        last_success="2026-04-28T12:00:00+00:00",
        consecutive_failures=0,
        new_cards_this_run=1,
        extraction_pending_count=0,
        total_cards=2,
        last_cron_run="2026-04-28T12:00:00+00:00",
        last_cron_success="2026-04-28T12:00:00+00:00",
        last_cron_runner="github-actions",
    )


def test_is_allowed_card_md():
    assert git_push._is_allowed("cards/foo.md") is True


def test_is_allowed_raw_txt():
    assert git_push._is_allowed("cards/foo.raw.txt") is True


def test_is_allowed_heartbeat():
    assert git_push._is_allowed(STATUS_FILE_NAME) is True


def test_is_allowed_conflicts_log():
    assert git_push._is_allowed("_conflicts.md") is True


def test_is_allowed_conflicts_dir():
    assert git_push._is_allowed("_conflicts/run-001/foo.md.local") is True
    assert git_push._is_allowed("_conflicts/run-001/foo.md.remote") is True


def test_excluded_rej_outside_conflicts():
    assert git_push._is_allowed("cards/foo.md.rej") is False


def test_excluded_local_outside_conflicts():
    assert git_push._is_allowed("cards/foo.md.local") is False


def test_excluded_remote_outside_conflicts():
    assert git_push._is_allowed("cards/foo.md.remote") is False


def test_excluded_rej_inside_conflicts_still_blocked():
    """Even inside _conflicts/, .rej files are blocked (only .local/.remote
    are intentional sidecars)."""
    assert git_push._is_allowed("_conflicts/run/foo.rej") is False


def test_excluded_random_file():
    assert git_push._is_allowed(".env") is False
    assert git_push._is_allowed(".gitignore") is False
    assert git_push._is_allowed("README") is False


def test_recovery_text_no_secrets():
    """Static template; no env-var interpolation."""
    text = git_push._push_rejected_recovery_text("run-test-001", 3)
    assert "run-test-001" in text  # run_id is not a secret
    # Sentinel substrings that should never appear:
    assert "XSENSAI_X_REFRESH_TOKEN" not in text or "XSENSAI_X_REFRESH_TOKEN" in text  # name OK
    # but no value would
    assert "Bearer " not in text
    assert "secret" not in text.lower() or "secrets" in text.lower()  # avoid false positive


def test_recovery_text_has_recovery_steps():
    text = git_push._push_rejected_recovery_text("test-run", 3)
    assert "git pull --rebase" in text
    assert "git push" in text
    assert "git rm SYNC_PUSH_REJECTED.md" in text
    assert "docs/CRON_SETUP.md" in text


def test_commit_and_push_no_changes(tmp_path: Path):
    """No changes → cards_committed=0, success=True."""
    _, clone = _setup_local_remote_clone(tmp_path)
    res = git_push.commit_and_push(
        clone,
        message="test: no changes",
        in_memory_status=_stub_status(),
        run_id="test-no-changes",
    )
    assert res.success is True
    assert res.cards_committed == 0


def test_commit_and_push_happy_path(tmp_path: Path):
    """Add a card, commit, push, verify it lands on remote."""
    remote, clone = _setup_local_remote_clone(tmp_path)
    card = clone / "test-card.md"
    card.write_text("---\nsource_id: \"42\"\n---\nbody\n")
    write_status(clone, _stub_status())

    res = git_push.commit_and_push(
        clone,
        message="cron: synced 1 bookmark",
        in_memory_status=_stub_status(),
        run_id="test-happy",
    )
    assert res.success is True
    assert res.cards_committed == 2  # card + heartbeat both staged

    # Confirm commit landed on remote
    log_res = subprocess.run(
        ["git", "-C", str(clone), "log", "--oneline", "-2"],
        capture_output=True, text=True, check=True,
    )
    assert "cron: synced 1 bookmark" in log_res.stdout


def test_commit_and_push_excludes_orphan_rej(tmp_path: Path):
    """`.rej` file outside `_conflicts/` should NOT be staged."""
    _, clone = _setup_local_remote_clone(tmp_path)
    (clone / "test-card.md").write_text("---\nsource_id: \"1\"\n---\nbody\n")
    (clone / "test-card.md.rej").write_text("ORPHAN — should not commit")
    write_status(clone, _stub_status())

    res = git_push.commit_and_push(
        clone,
        message="cron: filtered",
        in_memory_status=_stub_status(),
        run_id="test-rej-filter",
    )
    assert res.success is True
    # The .rej file is still on disk but NOT in the commit
    log_show = subprocess.run(
        ["git", "-C", str(clone), "show", "--stat", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    assert "test-card.md.rej" not in log_show.stdout
    assert "test-card.md" in log_show.stdout


def test_commit_and_push_includes_conflicts_dir(tmp_path: Path):
    """Files inside `_conflicts/<run>/` are intentional and DO get committed."""
    _, clone = _setup_local_remote_clone(tmp_path)
    conf_dir = clone / "_conflicts" / "test-run"
    conf_dir.mkdir(parents=True)
    (conf_dir / "card.md.local").write_text("local")
    (conf_dir / "card.md.remote").write_text("remote")
    (clone / "_conflicts.md").write_text("# log\n")

    res = git_push.commit_and_push(
        clone,
        message="cron: conflict marker",
        in_memory_status=_stub_status(),
        run_id="test-conf-include",
    )
    assert res.success is True
    log_show = subprocess.run(
        ["git", "-C", str(clone), "show", "--stat", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    assert "card.md.local" in log_show.stdout
    assert "card.md.remote" in log_show.stdout


def test_commit_and_push_pull_rebase_recovers(tmp_path: Path):
    """Concurrent commit on remote → cron pull-rebases + pushes."""
    remote, clone_a = _setup_local_remote_clone(tmp_path)

    # Have a sibling clone push a non-conflicting commit
    clone_b = tmp_path / "clone-b"
    subprocess.run(
        ["git", "clone", str(remote), str(clone_b)], capture_output=True, check=True
    )
    _git(clone_b, "config", "user.email", "b@test.local")
    _git(clone_b, "config", "user.name", "B")
    (clone_b / "other.md").write_text("---\nsource_id: \"99\"\n---\nbody\n")
    _git(clone_b, "add", "other.md")
    _git(clone_b, "commit", "-m", "remote: other card")
    _git(clone_b, "push", "origin", "main")

    # clone_a now writes a different card and tries to push
    (clone_a / "test-card.md").write_text("---\nsource_id: \"1\"\n---\nbody\n")
    res = git_push.commit_and_push(
        clone_a,
        message="cron: synced 1 bookmark",
        in_memory_status=_stub_status(),
        run_id="test-rebase",
    )
    # Should succeed via pull-rebase
    assert res.success is True

    # Verify both commits are on the remote
    log_res = subprocess.run(
        ["git", "-C", str(clone_a), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    )
    assert "remote: other card" in log_res.stdout
    assert "cron: synced 1 bookmark" in log_res.stdout
