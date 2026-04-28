"""Slice 6 — setup wizard tests.

Covers:
- Mutual-exclusion contract: exactly one mode flag required.
- Preflight detects missing binaries.
- State file persistence (skip-completed on --resume).
- Error envelopes for SETUP_GH_AUTH_REQUIRED.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from xsensai.entrypoints import setup_wizard


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect XDG_CACHE_HOME so setup-state.json is isolated per test."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path / "xsensai" / "setup-state.json"


def _run_wizard(args):
    return subprocess.run(
        [sys.executable, "-m", "xsensai.entrypoints.setup_wizard"] + args,
        capture_output=True, text=True,
    )


class TestMutualExclusion:
    def test_no_mode_fails(self, tmp_state):
        result = _run_wizard([])
        assert result.returncode != 0
        assert "required" in result.stderr.lower() or "one of" in result.stderr.lower()

    def test_two_modes_fails(self, tmp_state):
        result = _run_wizard(["--preflight", "--oauth"])
        assert result.returncode != 0


class TestPreflight:
    def test_preflight_state_persists(self, tmp_state, monkeypatch):
        # Make XDG_CACHE_HOME visible to subprocess
        env = os.environ.copy()
        env["XDG_CACHE_HOME"] = str(tmp_state.parent.parent)
        result = subprocess.run(
            [sys.executable, "-m", "xsensai.entrypoints.setup_wizard", "--preflight"],
            capture_output=True, text=True, env=env,
        )
        # Whether preflight passes depends on env; we just want state recorded.
        assert tmp_state.exists()
        state = json.loads(tmp_state.read_text())
        assert "preflight" in state.get("steps", {})
        assert state["steps"]["preflight"]["status"] in {"completed", "failed"}

    def test_preflight_detects_missing_qmd(self, tmp_state, monkeypatch):
        # Simulate qmd not being on PATH
        monkeypatch.setenv("XSENSAI_QMD_PATH", "/nonexistent/path")
        with patch("xsensai.entrypoints.setup_wizard.shutil.which") as mock_which:
            # gh + ssh-keygen present, qmd missing
            mock_which.side_effect = lambda x: "/usr/bin/" + x if x in ("gh", "ssh-keygen") else None
            state = setup_wizard._load_state()
            rc = setup_wizard.cmd_preflight(state)
        assert rc == 1


class TestSkipCompletedStep:
    def test_oauth_skipped_if_completed(self, tmp_state, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_state.parent.parent))
        # Pre-populate state as if oauth already done
        state = {
            "version": 1,
            "started_at": "2026-01-01T00:00:00+00:00",
            "steps": {"oauth": {"status": "completed", "ts": "2026-01-01T00:00:00+00:00"}},
        }
        tmp_state.parent.mkdir(parents=True, exist_ok=True)
        tmp_state.write_text(json.dumps(state))
        # Loading state and checking _step_done
        loaded = setup_wizard._load_state()
        assert setup_wizard._step_done(loaded, "oauth")


class TestErrorEnvelopes:
    def test_gh_auth_required_envelope_format(self):
        # Patch gh auth status to fail
        with patch("xsensai.entrypoints.setup_wizard.shutil.which") as mock_which, \
             patch("xsensai.entrypoints.setup_wizard.subprocess.run") as mock_run:
            mock_which.return_value = "/usr/bin/gh"
            mock_run.return_value.returncode = 1
            err = setup_wizard._ensure_gh_auth()
        assert err is not None
        rendered = err.format()
        assert "SETUP_GH_AUTH_REQUIRED" in rendered
        assert "gh auth login" in rendered
        assert "Retryable: yes" in rendered

    def test_deploy_key_rejected_envelope_format(self):
        # Construct via XSensaiError directly to verify the code is registered
        from xsensai.errors import XSensaiError
        err = XSensaiError(
            code="SETUP_DEPLOY_KEY_REJECTED",
            cause="GitHub rejected the deploy key (HTTP 422)",
            attempted="gh api -X POST repos/x/y/keys",
            next_action="ensure permission",
            retryable=True,
        )
        rendered = err.format()
        assert "SETUP_DEPLOY_KEY_REJECTED" in rendered

    def test_first_run_failed_envelope_format(self):
        from xsensai.errors import XSensaiError
        err = XSensaiError(
            code="SETUP_FIRST_RUN_FAILED",
            cause="workflow run reached FAILED",
            attempted="gh workflow run sync.yml",
            next_action="inspect logs",
            retryable=True,
        )
        rendered = err.format()
        assert "SETUP_FIRST_RUN_FAILED" in rendered


class TestNoRollbackJournalEnvelope:
    def test_envelope_registered(self):
        from xsensai.errors import XSensaiError
        err = XSensaiError(
            code="NO_ROLLBACK_JOURNAL",
            cause="Rollback journal not found",
            attempted="migrate_v1_to_v2 --rollback",
            next_action="ensure --apply ran first",
            retryable=False,
        )
        rendered = err.format()
        assert "NO_ROLLBACK_JOURNAL" in rendered
        assert "Retryable: no" in rendered
