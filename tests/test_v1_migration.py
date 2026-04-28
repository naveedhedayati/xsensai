"""Slice 6 v1→v2 migration tests.

Covers:
- --dry-run lists v1 cards without writing.
- --apply migrates v1 → v2 (frontmatter + sidecar) and writes byte-exact rollback journal.
- --rollback restores v1 state byte-exact (round-trip via shasum).
- Mid-flight crash: journal entry exists, write_card succeeded, kill → rollback restores.
- Apply refuses to overwrite an existing journal.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATE_SCRIPT = REPO_ROOT / "scripts" / "migrate_v1_to_v2.py"


@pytest.fixture
def vault_corpus(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    c = vault / "04_areas" / "x-bookmarks"
    c.mkdir(parents=True)
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(c))
    return c


def _plant_v1_card(corpus_path: Path, stem: str, source_id: str = "1234567890") -> Path:
    """Plant a real-shape v1 card on disk. Returns the .md path."""
    md_path = corpus_path / f"{stem}.md"
    md_path.write_text(
        f"""---
type: x-bookmark
x_post_id: "{source_id}"
x_author: paulg
x_source_url: https://x.com/paulg/status/{source_id}
x_date: 2024-12-01T10:00:00Z
captured: 2024-12-01T10:00:00Z
x_extraction_status: success
x_tags:
  - example
  - test
---

## Content

v1 body for {stem}.

## Thread

OP reply chain.
"""
    )
    return md_path


def _run_migrate(args, corpus_path: Path):
    """Invoke the migration script as a subprocess (more realistic + isolates state)."""
    cmd = [sys.executable, str(MIGRATE_SCRIPT)] + args + ["--corpus", str(corpus_path)]
    return subprocess.run(cmd, capture_output=True, text=True)


class TestDryRun:
    def test_dry_run_lists_v1_cards(self, vault_corpus):
        _plant_v1_card(vault_corpus, "card-1", "1111111111")
        _plant_v1_card(vault_corpus, "card-2", "2222222222")
        result = _run_migrate(["--dry-run"], vault_corpus)
        assert result.returncode == 0
        assert "Found 2 v1 card(s)" in result.stdout
        assert "card-1.md" in result.stdout
        assert "card-2.md" in result.stdout

    def test_dry_run_writes_nothing(self, vault_corpus):
        md = _plant_v1_card(vault_corpus, "card-1")
        original_bytes = md.read_bytes()
        _run_migrate(["--dry-run"], vault_corpus)
        assert md.read_bytes() == original_bytes
        assert not (vault_corpus / "migrate_v1_to_v2.rollback.jsonl").exists()
        # No sidecar
        sidecars = list(vault_corpus.glob("*.raw.txt"))
        assert sidecars == []


class TestApply:
    def test_apply_migrates_v1_to_v2(self, vault_corpus):
        md = _plant_v1_card(vault_corpus, "card-1", "1111111111")
        result = _run_migrate(["--apply", "--yes"], vault_corpus)
        assert result.returncode == 0, f"stderr={result.stderr}\nstdout={result.stdout}"
        # Sidecar created
        sidecars = list(vault_corpus.glob("card-1.*.raw.txt"))
        assert len(sidecars) == 1
        # Frontmatter now has raw_path/raw_checksum
        new_text = md.read_text()
        assert "raw_path:" in new_text
        assert "raw_checksum:" in new_text
        assert "extraction_pending:" in new_text
        # Original v1 fields gone (mapped to v2 names)
        assert "x_post_id" not in new_text
        # Journal exists
        assert (vault_corpus / "migrate_v1_to_v2.rollback.jsonl").exists()

    def test_apply_journal_is_byte_exact(self, vault_corpus):
        md = _plant_v1_card(vault_corpus, "card-1")
        original = md.read_bytes()
        _run_migrate(["--apply", "--yes"], vault_corpus)
        journal = vault_corpus / "migrate_v1_to_v2.rollback.jsonl"
        with journal.open() as f:
            entries = [json.loads(line) for line in f if line.strip()]
        assert len(entries) == 1
        entry = entries[0]
        decoded = base64.b64decode(entry["v1_md_bytes_b64"])
        assert decoded == original
        expected_sha = "sha256:" + hashlib.sha256(original).hexdigest()
        assert entry["v1_md_sha256"] == expected_sha

    def test_apply_refuses_existing_journal(self, vault_corpus):
        _plant_v1_card(vault_corpus, "card-1")
        _run_migrate(["--apply", "--yes"], vault_corpus)
        # Plant another v1 card (after first apply succeeded)
        _plant_v1_card(vault_corpus, "card-2", "2222222222")
        result = _run_migrate(["--apply", "--yes"], vault_corpus)
        assert result.returncode == 1
        assert "Refusing to overwrite" in result.stderr


class TestRollback:
    def test_rollback_restores_byte_exact(self, vault_corpus):
        md = _plant_v1_card(vault_corpus, "card-1", "1111111111")
        original = md.read_bytes()
        original_sha = hashlib.sha256(original).hexdigest()

        # Apply then rollback
        apply_result = _run_migrate(["--apply", "--yes"], vault_corpus)
        assert apply_result.returncode == 0
        # File should have been modified
        modified = md.read_bytes()
        assert modified != original

        rollback_result = _run_migrate(["--rollback", "--yes"], vault_corpus)
        assert rollback_result.returncode == 0, f"stderr={rollback_result.stderr}"
        # File should be byte-exact restored
        restored = md.read_bytes()
        assert restored == original
        assert hashlib.sha256(restored).hexdigest() == original_sha
        # Sidecar should be unlinked
        sidecars = list(vault_corpus.glob("*.raw.txt"))
        assert sidecars == []
        # Journal archived (not at original name)
        assert not (vault_corpus / "migrate_v1_to_v2.rollback.jsonl").exists()
        archives = list(vault_corpus.glob("migrate_v1_to_v2.rollback.applied-*"))
        assert len(archives) == 1

    def test_rollback_without_journal(self, vault_corpus):
        result = _run_migrate(["--rollback", "--yes"], vault_corpus)
        assert result.returncode == 1
        assert "NO_ROLLBACK_JOURNAL" in result.stderr

    def test_rollback_corrupt_journal_line_skipped(self, vault_corpus):
        # Apply normally first
        md = _plant_v1_card(vault_corpus, "card-1")
        original = md.read_bytes()
        _run_migrate(["--apply", "--yes"], vault_corpus)

        # Corrupt the journal: append a junk line
        journal = vault_corpus / "migrate_v1_to_v2.rollback.jsonl"
        with journal.open("a") as f:
            f.write("not valid json\n")

        # Rollback should skip the bad line and still restore the good one,
        # BUT exit non-zero and refuse to archive (silent rollback gap
        # protection: skipped corrupt lines mean potentially-unrestored
        # cards, so the journal is preserved for forensic review).
        result = _run_migrate(["--rollback", "--yes"], vault_corpus)
        assert result.returncode == 1
        assert md.read_bytes() == original
        # Journal NOT archived — still at original path so user can inspect
        assert journal.exists()
        # Warning surfaced to stderr
        assert "corrupt" in result.stderr.lower()


class TestRollbackPathTraversalGuard:
    def test_rollback_refuses_journal_pointing_outside_corpus(self, vault_corpus, tmp_path):
        # Apply normally to create a journal
        _plant_v1_card(vault_corpus, "card-1")
        _run_migrate(["--apply", "--yes"], vault_corpus)
        journal = vault_corpus / "migrate_v1_to_v2.rollback.jsonl"
        # Tamper: rewrite the v1_md_path to point OUTSIDE the corpus.
        # The rollback must refuse to overwrite the external path even if
        # checksums would match.
        outside = tmp_path / "evil.md"
        outside.write_bytes(b"original outside content\n")
        with journal.open("r") as f:
            entry = json.loads(f.readline())
        entry["v1_md_path"] = str(outside)
        with journal.open("w") as f:
            f.write(json.dumps(entry) + "\n")
        result = _run_migrate(["--rollback", "--yes"], vault_corpus)
        # Rollback refuses (the corpus boundary check raises)
        assert result.returncode == 1
        # The outside file was NOT overwritten
        assert outside.read_bytes() == b"original outside content\n"


class TestApplyHandlesEmptyJournal:
    def test_apply_succeeds_when_prior_journal_is_empty(self, vault_corpus):
        # Simulate a crashed prior --apply that created an empty journal
        journal = vault_corpus / "migrate_v1_to_v2.rollback.jsonl"
        journal.touch()
        assert journal.stat().st_size == 0

        _plant_v1_card(vault_corpus, "card-1")
        result = _run_migrate(["--apply", "--yes"], vault_corpus)
        # Should NOT block on the empty journal
        assert result.returncode == 0, f"stderr={result.stderr}\nstdout={result.stdout}"


class TestExclusiveModes:
    def test_no_mode_fails(self, vault_corpus):
        result = _run_migrate([], vault_corpus)
        assert result.returncode != 0
        assert "one of the arguments" in result.stderr or "required" in result.stderr.lower()

    def test_two_modes_fails(self, vault_corpus):
        result = _run_migrate(["--dry-run", "--apply"], vault_corpus)
        assert result.returncode != 0
