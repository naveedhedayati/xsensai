"""Documentation CI grep — DX9 fix.

Catches doc drift at PR time, not 6 months later. For each shipped feature
of Slice 3, assert the documentation anchors exist where the plan said
they would.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "rel_path,pattern,description",
    [
        ("commands/xhelp.md", r"/xask\b.*\blive\b", "/xhelp lists /xask as live"),
        ("CLAUDE.md", r"/xask", "CLAUDE.md documents /xask in the command map"),
        (
            "commands/xask.md",
            r"override vocabulary|no decay",
            "commands/xask.md documents /xask override vocabulary",
        ),
        ("README.md", r"/xask", "README.md mentions /xask"),
        ("TROUBLESHOOTING.md", r"/xask", "TROUBLESHOOTING.md has a /xask section"),
        ("CHANGELOG.md", r"v?0\.4\.0\.0", "CHANGELOG.md has v0.4.0.0 entry"),
    ],
)
def test_xask_documented_everywhere(rel_path: str, pattern: str, description: str):
    p = repo_root() / rel_path
    assert p.exists(), f"missing doc file: {rel_path}"
    text = p.read_text(encoding="utf-8")
    assert re.search(pattern, text, re.IGNORECASE), (
        f"{description}: regex {pattern!r} not found in {rel_path}"
    )


@pytest.mark.parametrize(
    "token",
    ["no decay", "skip pins", "no web", "challenge"],
)
def test_override_vocabulary_documented(token: str):
    """All 4 /xask override tokens must appear in commands/xask.md AND commands/xhelp.md.

    (Post-CLAUDE.md-rewrite: the override vocabulary is a /xask command detail, so
    it lives in the command docs, not in the slimmed project-instructions file.)
    """
    for rel in ("commands/xask.md", "commands/xhelp.md"):
        p = repo_root() / rel
        text = p.read_text(encoding="utf-8")
        assert token in text, (
            f"override token {token!r} missing from {rel} — DX3 documentation gap"
        )


@pytest.mark.parametrize(
    "env_var",
    [
        "XSENSAI_LAST30DAYS_PATH",
        "XSENSAI_XASK_WEB_TIMEOUT_S",
        "XSENSAI_XASK_LOG_MODE",
        "XSENSAI_XASK_LOG_RETENTION_DAYS",
    ],
)
def test_env_vars_documented_in_claude_md(env_var: str):
    p = repo_root() / "CLAUDE.md"
    text = p.read_text(encoding="utf-8")
    assert env_var in text, f"{env_var} not documented in CLAUDE.md"


def test_deploy_status_includes_xask_capabilities():
    """CLAUDE.md deploy-status enumeration must include the new MCP tool."""
    p = repo_root() / "CLAUDE.md"
    text = p.read_text(encoding="utf-8")
    assert "xask_capabilities" in text, (
        "CLAUDE.md deploy-status didn't pick up xask_capabilities (EC9)"
    )
