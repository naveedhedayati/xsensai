"""Slice 6 — shadow union merge tests.

The shadow resolver computes a candidate merge AND logs it to _conflicts.md
but never changes the actual rebase outcome (fail-loud stays primary in
Slice 6). Tests target:
- compute_union_candidate correctness (spec-literal: prefer-local + list union).
- append_shadow_union_log idempotency (per /autoplan eng-review: 3-retry loop
  must not double-log).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xsensai.sync import git_merge


def _make_card(frontmatter_yaml: str, body: str) -> bytes:
    return f"---\n{frontmatter_yaml}---\n\n{body}\n".encode("utf-8")


class TestComputeUnionCandidate:
    def test_prefer_local_on_scalar_collision(self):
        local = _make_card(
            "source_type: paste\ncaptured: 2026-04-28T10:00:00+00:00\nwhy_saved: local-reason\n",
            "## Content\n\nlocal body",
        )
        remote = _make_card(
            "source_type: paste\ncaptured: 2026-04-28T10:00:00+00:00\nwhy_saved: remote-reason\n",
            "## Content\n\nremote body",
        )
        merged_bytes, diff = git_merge.compute_union_candidate(local, remote, None)
        text = merged_bytes.decode("utf-8")
        # Frontmatter: local why_saved wins
        assert "why_saved: local-reason" in text
        # Body: prefer local
        assert "local body" in text
        assert "remote body" not in text

    def test_list_union_with_order_preservation(self):
        local = _make_card(
            "source_type: paste\ncaptured: 2026-04-28T10:00:00+00:00\ntags:\n- a\n- b\n",
            "x",
        )
        remote = _make_card(
            "source_type: paste\ncaptured: 2026-04-28T10:00:00+00:00\ntags:\n- b\n- c\n",
            "x",
        )
        merged_bytes, diff = git_merge.compute_union_candidate(local, remote, None)
        text = merged_bytes.decode("utf-8")
        # Union should contain a, b, c (deduplicated)
        for tag in ("a", "b", "c"):
            assert f"- {tag}" in text
        assert "tags" in diff["would_have_merged"]

    def test_remote_only_field_preserved(self):
        local = _make_card(
            "source_type: paste\ncaptured: 2026-04-28T10:00:00+00:00\n",
            "x",
        )
        remote = _make_card(
            "source_type: paste\ncaptured: 2026-04-28T10:00:00+00:00\nretrieval_summary: only-remote\n",
            "x",
        )
        merged_bytes, diff = git_merge.compute_union_candidate(local, remote, None)
        text = merged_bytes.decode("utf-8")
        assert "retrieval_summary: only-remote" in text
        assert "retrieval_summary" in diff["would_have_merged"]

    def test_byte_size_diagnostics(self):
        local = _make_card("source_type: paste\ncaptured: 2026-04-28T10:00:00+00:00\n", "x")
        remote = local
        _, diff = git_merge.compute_union_candidate(local, remote, None)
        assert diff["byte_size_local"] == len(local)
        assert diff["byte_size_remote"] == len(remote)
        assert diff["byte_size_union"] > 0


class TestAppendShadowUnionLogIdempotency:
    def test_first_call_writes_entry(self, tmp_path: Path):
        corpus_path = tmp_path / "corpus"
        corpus_path.mkdir()
        diff = {"would_have_merged": ["x"], "would_have_dropped": [], "byte_size_local": 100, "byte_size_remote": 100, "byte_size_union": 110}
        appended = git_merge.append_shadow_union_log(
            corpus_path, run_id="run-1", card_path="card-a.md", diff_summary=diff,
        )
        assert appended is True
        log_path = corpus_path / "_conflicts.md"
        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        assert "run-1" in content
        assert "card-a.md" in content

    def test_retry_does_not_double_log(self, tmp_path: Path):
        """Per /autoplan eng-review: commit_and_push retries up to 3x; the
        shadow log must dedup on (run_id, card_path)."""
        corpus_path = tmp_path / "corpus"
        corpus_path.mkdir()
        diff = {"would_have_merged": [], "would_have_dropped": [], "byte_size_local": 1, "byte_size_remote": 1, "byte_size_union": 1}
        # Three attempts — same (run_id, card_path)
        for _ in range(3):
            git_merge.append_shadow_union_log(
                corpus_path, run_id="run-1", card_path="card-a.md", diff_summary=diff,
            )
        log_path = corpus_path / "_conflicts.md"
        content = log_path.read_text(encoding="utf-8")
        # Only one entry for (run-1, card-a.md)
        lines = [ln for ln in content.splitlines() if ln.strip()]
        run1_card_a = [ln for ln in lines if "run-1" in ln and "card-a.md" in ln]
        assert len(run1_card_a) == 1, f"Expected 1 entry, got {len(run1_card_a)}: {run1_card_a}"

    def test_different_run_id_writes_new_entry(self, tmp_path: Path):
        corpus_path = tmp_path / "corpus"
        corpus_path.mkdir()
        diff = {"would_have_merged": [], "would_have_dropped": [], "byte_size_local": 1, "byte_size_remote": 1, "byte_size_union": 1}
        git_merge.append_shadow_union_log(
            corpus_path, run_id="run-1", card_path="card-a.md", diff_summary=diff,
        )
        git_merge.append_shadow_union_log(
            corpus_path, run_id="run-2", card_path="card-a.md", diff_summary=diff,
        )
        log_path = corpus_path / "_conflicts.md"
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 2
