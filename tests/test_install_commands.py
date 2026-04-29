"""Slice 7.5 (v0.9.0.0) — install_commands.sh permissions.ask wiring tests.

Covers `scripts/_settings_merge.py` (the helper invoked by install_commands.sh
to wire the cryptographic gate for /xdelete + /xrestore via permissions.ask).

Per AE3 (autoplan eng phase): pytest + subprocess on a tmp HOME, not a manual
gauntlet. Covers:
- Empty / missing settings.json → file created with the entries
- Existing file with other keys → block added, keys preserved
- Idempotent re-run → no changes
- Malformed JSON → backup written, install continues
- Pre-existing wildcard in permissions.allow → loud warning printed
- Top-level not an object → safe-skip
- permissions.ask not an array → safe-skip
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "_settings_merge.py"


def _run(target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HELPER), str(target)],
        capture_output=True,
        text=True,
    )


class TestEmptyAndMissing:
    def test_missing_file_is_created(self, tmp_path):
        target = tmp_path / ".claude" / "settings.json"
        assert not target.exists()
        result = _run(target)
        assert result.returncode == 0, result.stderr
        assert target.exists()
        data = json.loads(target.read_text())
        assert data["permissions"]["ask"] == [
            "mcp__xsensai__delete_bookmark",
            "mcp__xsensai__restore_bookmark",
        ]
        assert "Created" in result.stdout

    def test_announces_creation(self, tmp_path):
        target = tmp_path / "settings.json"
        result = _run(target)
        assert "permissions.ask entries" in result.stdout
        assert "delete_bookmark" in result.stdout
        assert "restore_bookmark" in result.stdout


class TestPreservesExistingKeys:
    def test_existing_keys_preserved(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({
            "OPENAI_API_KEY": "sk-test",
            "permissions": {"allow": ["bash"]},
            "hooks": {"SessionStart": []},
            "agentPushNotifEnabled": True,
        }))
        result = _run(target)
        assert result.returncode == 0, result.stderr
        data = json.loads(target.read_text())
        assert data["OPENAI_API_KEY"] == "sk-test"
        assert data["permissions"]["allow"] == ["bash"]
        assert data["permissions"]["ask"] == [
            "mcp__xsensai__delete_bookmark",
            "mcp__xsensai__restore_bookmark",
        ]
        assert data["hooks"] == {"SessionStart": []}
        assert data["agentPushNotifEnabled"] is True

    def test_existing_ask_entries_appended_not_replaced(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({
            "permissions": {"ask": ["mcp__some_other_tool"]},
        }))
        _run(target)
        data = json.loads(target.read_text())
        ask = data["permissions"]["ask"]
        assert "mcp__some_other_tool" in ask
        assert "mcp__xsensai__delete_bookmark" in ask
        assert "mcp__xsensai__restore_bookmark" in ask


class TestIdempotency:
    def test_running_twice_no_duplicates(self, tmp_path):
        target = tmp_path / "settings.json"
        _run(target)
        _run(target)
        data = json.loads(target.read_text())
        ask = data["permissions"]["ask"]
        assert ask.count("mcp__xsensai__delete_bookmark") == 1
        assert ask.count("mcp__xsensai__restore_bookmark") == 1

    def test_idempotent_announces_no_changes(self, tmp_path):
        target = tmp_path / "settings.json"
        _run(target)
        result = _run(target)
        assert result.returncode == 0
        assert "no changes" in result.stdout.lower() or "already present" in result.stdout.lower()

    def test_idempotent_no_backup_on_no_op(self, tmp_path):
        target = tmp_path / "settings.json"
        _run(target)
        backups_before = list(target.parent.glob("settings.json.bak.*"))
        _run(target)
        backups_after = list(target.parent.glob("settings.json.bak.*"))
        # First run created one backup (because target started missing →
        # actually NO — for missing, no backup is made. Re-runs that already
        # have entries should also make no backup).
        assert len(backups_after) == len(backups_before)


class TestMalformedJSON:
    def test_malformed_file_safe_skip(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text("{ this is not valid json")
        result = _run(target)
        assert result.returncode == 0  # safe-skip, don't kill install
        assert "[SETTINGS_MALFORMED]" in result.stdout
        # Backup created
        backups = list(target.parent.glob("settings.json.bak.*"))
        assert len(backups) == 1
        # Original file is untouched
        assert target.read_text() == "{ this is not valid json"

    def test_top_level_not_object_safe_skip(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps([1, 2, 3]))
        result = _run(target)
        assert result.returncode == 0
        assert "[SETTINGS_MALFORMED]" in result.stdout

    def test_permissions_not_object_safe_skip(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"permissions": "wat"}))
        result = _run(target)
        assert result.returncode == 0
        assert "[SETTINGS_MALFORMED]" in result.stdout

    def test_ask_not_array_safe_skip(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"permissions": {"ask": "not-an-array"}}))
        result = _run(target)
        assert result.returncode == 0
        assert "[SETTINGS_MALFORMED]" in result.stdout


class TestWildcardOverride:
    def test_wildcard_in_allow_warns(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({
            "permissions": {"allow": ["mcp__*"]},
        }))
        result = _run(target)
        assert result.returncode == 0
        assert "[PERMISSIONS_WILDCARD_OVERRIDE]" in result.stdout
        assert "mcp__xsensai__delete_bookmark" in result.stdout
        # Entries still added so the user can fix the allow wildcard
        # without having to also re-run install.
        data = json.loads(target.read_text())
        assert "mcp__xsensai__delete_bookmark" in data["permissions"]["ask"]

    def test_literal_in_allow_warns(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({
            "permissions": {"allow": ["mcp__xsensai__delete_bookmark"]},
        }))
        result = _run(target)
        assert "[PERMISSIONS_WILDCARD_OVERRIDE]" in result.stdout

    def test_no_warn_when_no_wildcard(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({
            "permissions": {"allow": ["bash", "edit"]},
        }))
        result = _run(target)
        assert "[PERMISSIONS_WILDCARD_OVERRIDE]" not in result.stdout

    def test_partial_wildcard_match(self, tmp_path):
        target = tmp_path / "settings.json"
        # "mcp__xsensai__*" should subsume our literal entries.
        target.write_text(json.dumps({
            "permissions": {"allow": ["mcp__xsensai__*"]},
        }))
        result = _run(target)
        assert "[PERMISSIONS_WILDCARD_OVERRIDE]" in result.stdout


class TestBackup:
    def test_backup_on_real_change(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"existing": "value"}))
        _run(target)
        backups = list(target.parent.glob("settings.json.bak.*"))
        assert len(backups) == 1
        # Backup contains the original
        original_data = json.loads(backups[0].read_text())
        assert original_data == {"existing": "value"}

    def test_no_backup_on_create(self, tmp_path):
        target = tmp_path / "settings.json"
        _run(target)  # file didn't exist
        backups = list(target.parent.glob("settings.json.bak.*"))
        assert backups == []

    def test_backup_chmod_0600(self, tmp_path):
        """Slice 7.5 /review F9: backups contain settings (often API keys)
        and MUST be mode 0600 — not world-readable.
        """
        import stat
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"OPENAI_API_KEY": "sk-secret"}))
        _run(target)
        backups = list(target.parent.glob("settings.json.bak.*"))
        assert len(backups) == 1
        mode = backups[0].stat().st_mode & 0o777
        assert mode == 0o600, f"backup mode is {oct(mode)}, expected 0o600"

    def test_settings_chmod_0600(self, tmp_path):
        """Atomic write helper opens with mode 0600 — settings file containing
        secrets should not be world-readable even briefly.
        """
        target = tmp_path / "settings.json"
        _run(target)
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600, f"settings mode is {oct(mode)}, expected 0o600"

    def test_backup_retention_caps_at_max(self, tmp_path):
        """Slice 7.5 /review F9: unbounded backup accumulation is a privacy
        concern. Helper retains only MAX_BACKUPS=3 most recent.
        """
        target = tmp_path / "settings.json"
        # Trigger 5 real changes by toggling a key between runs.
        for i in range(5):
            target.write_text(json.dumps({"key": f"value-{i}"}))
            # If permissions.ask already has our entries, no change is made
            # and no new backup written. Toggle the ask state by removing it.
            data = json.loads(target.read_text())
            data.pop("permissions", None)
            target.write_text(json.dumps(data))
            _run(target)
        backups = list(target.parent.glob("settings.json.bak.*"))
        assert len(backups) <= 3, f"expected <=3 backups, got {len(backups)}"


class TestEdgeCasesAndHardening:
    """Slice 7.5 /review hardening — non-string allow entries, large file,
    directory-as-target, and other edge cases the original test suite missed.
    """

    def test_non_string_allow_entries_safe(self, tmp_path):
        """/review F4: malformed allow entries (int, dict, null) must not
        crash the wildcard subsumption check.
        """
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({
            "permissions": {"allow": [42, {"glob": "x"}, None, "bash"]},
        }))
        result = _run(target)
        assert result.returncode == 0, result.stderr
        # Helper still installed the entries
        data = json.loads(target.read_text())
        assert "mcp__xsensai__delete_bookmark" in data["permissions"]["ask"]
        # No crash trace
        assert "AttributeError" not in result.stdout
        assert "AttributeError" not in result.stderr

    def test_large_file_safe_skip(self, tmp_path):
        """/review F8: a 1+ MB settings.json safe-skips with a clear envelope
        rather than spending cycles parsing JSON garbage.
        """
        target = tmp_path / "settings.json"
        # Generate a >1 MB file. Use repeated valid JSON so we test the size
        # cap, not the malformed-JSON path.
        big_payload = '{"key":"' + ("x" * 1_100_000) + '"}'
        target.write_text(big_payload)
        result = _run(target)
        assert result.returncode == 0
        assert "[SETTINGS_MALFORMED]" in result.stdout
        assert "larger than 1 MB" in result.stdout

    def test_target_is_directory_safe_skip(self, tmp_path):
        """/review F-test: target being a directory (not a file) must
        safe-skip rather than crash.
        """
        target = tmp_path / "settings.json"
        target.mkdir()
        result = _run(target)
        assert result.returncode == 0
        assert "[SETTINGS_MALFORMED]" in result.stdout

    def test_concurrent_runs_dont_lose_entries(self, tmp_path):
        """/review F3: flock + atomic write must survive two parallel installs.
        Run two processes concurrently against the same settings file; both
        should complete and the final file should contain BOTH ASK_ENTRIES
        plus the pre-existing key.
        """
        import concurrent.futures

        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"existing": "value"}))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futures = [ex.submit(_run, target) for _ in range(2)]
            results = [f.result() for f in futures]

        # Both runs exited 0
        for r in results:
            assert r.returncode == 0, r.stderr
        # Final file is valid JSON with all ASK_ENTRIES + the original key
        data = json.loads(target.read_text())
        assert data["existing"] == "value"
        ask = data["permissions"]["ask"]
        assert "mcp__xsensai__delete_bookmark" in ask
        assert "mcp__xsensai__restore_bookmark" in ask
        # No duplicates from racing reads
        assert ask.count("mcp__xsensai__delete_bookmark") == 1
        assert ask.count("mcp__xsensai__restore_bookmark") == 1
