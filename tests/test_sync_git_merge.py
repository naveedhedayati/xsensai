"""Slice 5 — git_merge tests: heartbeat fast-path + card conflict fail-loud.

Uses real local git fixtures (two divergent clones against a bare remote)
because the conflict surface is genuinely git-mechanic; mocking would
miss the spike #8 finding (need reset --hard between abort and
write-sidecars).

Gated on git presence — every CI runner has git so always runs.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Tuple

import pytest

from xsensai.sync import git_merge
from xsensai.sync.heartbeat import (
    STATUS_FILE_NAME,
    SyncStatus,
    write_status,
)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _setup_diverged_repos(tmp_path: Path) -> Tuple[Path, Path]:
    """Build a bare remote + two clones with a card conflict.

    Returns (remote_dir, clone_a, clone_b) where clone_a is "behind" and
    needs to pull-rebase clone_b's pushed commit; that pull-rebase will
    conflict on the shared card file.
    """
    remote = tmp_path / "remote.git"
    # --initial-branch=main pins HEAD to refs/heads/main on the bare repo so
    # CI runners (whose `init.defaultBranch` may still be "master") clone
    # the right tree after the first push. Without this, clone-b clones an
    # empty working tree because HEAD points to a branch that never gets
    # written.
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        capture_output=True, check=True,
    )

    clone_a = tmp_path / "clone-a"
    clone_b = tmp_path / "clone-b"
    subprocess.run(
        ["git", "clone", str(remote), str(clone_a)], capture_output=True, check=True
    )
    _git(clone_a, "config", "user.email", "a@test.local")
    _git(clone_a, "config", "user.name", "Clone A")

    # Create initial card + heartbeat
    card = clone_a / "cards" / "test-card.md"
    card.parent.mkdir()
    card.write_text(
        "---\nsource_id: \"123\"\nbody: original\n---\nbody original\n"
    )
    status = SyncStatus(
        last_run="2026-04-26T07:00:00+00:00",
        last_success="2026-04-26T07:00:00+00:00",
        consecutive_failures=0,
        new_cards_this_run=0,
        extraction_pending_count=0,
        total_cards=1,
    )
    write_status(clone_a, status)
    _git(clone_a, "add", "-A")
    _git(clone_a, "commit", "-m", "init")
    _git(clone_a, "branch", "-M", "main")
    _git(clone_a, "push", "-u", "origin", "main")

    subprocess.run(
        ["git", "clone", str(remote), str(clone_b)], capture_output=True, check=True
    )
    _git(clone_b, "config", "user.email", "b@test.local")
    _git(clone_b, "config", "user.name", "Clone B")

    # Clone B (pretend = remote/cron) edits the card + heartbeat, pushes.
    (clone_b / "cards" / "test-card.md").write_text(
        "---\nsource_id: \"123\"\nbody: cron-version\n---\nbody cron version\n"
    )
    cron_status = SyncStatus(
        last_run="2026-04-28T07:00:00+00:00",
        last_success="2026-04-28T07:00:00+00:00",
        consecutive_failures=0,
        new_cards_this_run=1,
        extraction_pending_count=1,
        total_cards=2,
        last_cron_run="2026-04-28T07:00:00+00:00",
        last_cron_success="2026-04-28T07:00:00+00:00",
        last_cron_runner="github-actions",
    )
    write_status(clone_b, cron_status)
    _git(clone_b, "add", "-A")
    _git(clone_b, "commit", "-m", "cron: update")
    _git(clone_b, "push", "origin", "main")

    # Clone A (pretend = local/manual) edits the same card differently,
    # commits but does not push.
    (clone_a / "cards" / "test-card.md").write_text(
        "---\nsource_id: \"123\"\nbody: user-version\npinned: true\n---\nbody user version\n"
    )
    _git(clone_a, "commit", "-am", "user: pin + edit")
    return clone_a, clone_b


def test_parse_porcelain_v2_conflicts_simple():
    output = (
        b"u UU N... 100644 100644 100644 100644 "
        b"abcd1234 efgh5678 ijkl9012 cards/test.md\x00"
        b"1 .M N... 100644 100644 100644 abcd efgh other.md\x00"
    )
    paths = git_merge.parse_porcelain_v2_conflicts(output)
    assert paths == ["cards/test.md"]


def test_parse_porcelain_v2_conflicts_empty():
    assert git_merge.parse_porcelain_v2_conflicts(b"") == []


def test_parse_porcelain_v2_conflicts_no_unmerged():
    """No 'u' rows = no conflicts; even with other staged changes."""
    output = b"1 .M N... 100644 100644 100644 abcd efgh new.md\x00"
    assert git_merge.parse_porcelain_v2_conflicts(output) == []


def test_classify_conflict():
    assert git_merge.classify_conflict("_sync-status.md") == "heartbeat"
    assert git_merge.classify_conflict("nested/_sync-status.md") == "heartbeat"
    assert git_merge.classify_conflict("cards/foo.md") == "card"
    assert git_merge.classify_conflict("cards/foo.raw.txt") == "raw_sidecar"
    assert git_merge.classify_conflict("README") == "other"


def test_card_conflict_failloud_workflow(tmp_path: Path):
    """End-to-end: trigger conflict, run failloud, verify sequence."""
    clone_a, _ = _setup_diverged_repos(tmp_path)

    # Trigger pull-rebase. Use core.editor=true so the rebase doesn't
    # try to launch an interactive editor on conflict.
    rebase = subprocess.run(
        ["git", "-C", str(clone_a), "-c", "core.editor=true", "pull",
         "--rebase", "origin", "main"],
        capture_output=True, text=True,
    )
    assert rebase.returncode != 0  # conflict expected

    # Sanity: conflict detected via porcelain v2.
    porcelain = subprocess.run(
        ["git", "-C", str(clone_a), "status", "--porcelain=v2", "-z"],
        capture_output=True, check=True,
    )
    paths = git_merge.parse_porcelain_v2_conflicts(porcelain.stdout)
    assert "cards/test-card.md" in paths

    # Run the fail-loud resolver.
    results = git_merge.resolve_card_conflict_failloud(
        clone_a,
        conflicting_paths=paths,
        run_id="spike-test-001",
    )
    # All cards should be resolved=False (manual review required).
    card_results = [r for r in results if r.kind == "card"]
    assert len(card_results) == 1
    assert card_results[0].resolved is False
    assert "spike-test-001" in card_results[0].note

    # Sidecars exist.
    conflicts_dir = clone_a / "_conflicts" / "spike-test-001"
    assert conflicts_dir.exists()
    assert (conflicts_dir / "test-card.md.local").exists()
    assert (conflicts_dir / "test-card.md.remote").exists()
    # Local sidecar carries user's version
    assert b"user version" in (conflicts_dir / "test-card.md.local").read_bytes()
    # Remote sidecar carries cron's version
    assert b"cron version" in (conflicts_dir / "test-card.md.remote").read_bytes()

    # _conflicts.md marker exists with the run_id.
    marker = clone_a / "_conflicts.md"
    assert marker.exists()
    assert "spike-test-001" in marker.read_text()
    assert "docs/CONFLICT_RESOLUTION.md" in marker.read_text()

    # Worktree is now matching origin/main + new staged conflict files
    # (i.e., no rebase in progress).
    assert not (clone_a / ".git" / "rebase-merge").exists()
    assert not (clone_a / ".git" / "rebase-apply").exists()


def test_card_conflict_marker_appends_on_repeat(tmp_path: Path):
    """Second conflict on the same vault appends to existing _conflicts.md."""
    clone_a, _ = _setup_diverged_repos(tmp_path)
    # Pre-seed an existing _conflicts.md with one prior entry.
    (clone_a / "_conflicts.md").write_text(
        "# x-sensai conflict log\n\n## 2026-04-25T07:00:00+00:00 — run_id=earlier\n"
        "\n1 unresolved conflict(s):\n\n- cards/old.md → ...\n"
    )

    subprocess.run(
        ["git", "-C", str(clone_a), "-c", "core.editor=true", "pull",
         "--rebase", "origin", "main"],
        capture_output=True, text=True,
    )
    porcelain = subprocess.run(
        ["git", "-C", str(clone_a), "status", "--porcelain=v2", "-z"],
        capture_output=True, check=True,
    )
    paths = git_merge.parse_porcelain_v2_conflicts(porcelain.stdout)
    git_merge.resolve_card_conflict_failloud(
        clone_a,
        conflicting_paths=paths,
        run_id="spike-test-002",
    )
    marker_text = (clone_a / "_conflicts.md").read_text()
    # Both entries present.
    assert "earlier" in marker_text
    assert "spike-test-002" in marker_text


def test_heartbeat_fast_path(tmp_path: Path):
    """Verify the heartbeat fast-path can reproduce a clean state during
    a real rebase conflict (won't actually trigger conflict on heartbeat
    here — just verifies the regenerate-from-memory + add path)."""
    clone_a, _ = _setup_diverged_repos(tmp_path)

    in_mem = SyncStatus(
        last_run="2026-04-28T12:00:00+00:00",
        last_success="2026-04-28T12:00:00+00:00",
        consecutive_failures=0,
        new_cards_this_run=2,
        extraction_pending_count=0,
        total_cards=4,
        last_cron_run="2026-04-28T12:00:00+00:00",
        last_cron_runner="github-actions",
    )
    res = git_merge.resolve_heartbeat_fast_path(clone_a, in_mem)
    assert res.kind == "heartbeat"
    assert res.resolved is True
    # File reflects in-memory state
    written = (clone_a / STATUS_FILE_NAME).read_text()
    assert "2026-04-28T12:00:00+00:00" in written
    assert "github-actions" in written


def test_path_traversal_rejected(tmp_path: Path):
    """resolve_card_conflict_failloud must validate paths against corpus."""
    clone_a, _ = _setup_diverged_repos(tmp_path)
    # Inject an evil porcelain conflict path that escapes the corpus.
    with pytest.raises(Exception):  # _assert_inside_corpus raises XSensaiError
        git_merge.resolve_card_conflict_failloud(
            clone_a,
            conflicting_paths=["../../../etc/passwd"],
            run_id="evil-traversal-test",
        )
