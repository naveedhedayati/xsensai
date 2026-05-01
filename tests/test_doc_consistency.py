"""Slice 4 D-6 fix: doc-consistency test.

Asserts the new /xsync and /xextract slash commands are documented across
ALL the user-facing surfaces. Catches the rot pattern where a new command
ships but xhelp.md still calls it "planned" + README still says "current
slice is N-1."
"""

from __future__ import annotations

from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent

DOC_FILES = {
    "CLAUDE.md": PROJECT_ROOT / "CLAUDE.md",
    "README.md": PROJECT_ROOT / "README.md",
    "TROUBLESHOOTING.md": PROJECT_ROOT / "TROUBLESHOOTING.md",
    "CHANGELOG.md": PROJECT_ROOT / "CHANGELOG.md",
    "commands/xhelp.md": PROJECT_ROOT / "commands" / "xhelp.md",
}


@pytest.fixture(scope="module")
def docs():
    return {name: path.read_text(encoding="utf-8") for name, path in DOC_FILES.items()}


def test_xsync_documented_everywhere(docs):
    """`/xsync` must appear in every user-facing doc (Slice 4 ships it)."""
    for name, content in docs.items():
        assert "/xsync" in content, f"{name} doesn't mention /xsync"


def test_xextract_documented_everywhere(docs):
    """`/xextract` is brand new in Slice 4 (D-S1 fix). Must appear everywhere."""
    for name, content in docs.items():
        assert "/xextract" in content, f"{name} doesn't mention /xextract"


def test_xhelp_lists_xsync_in_available_table(docs):
    """xhelp.md's 'Available now' table must list /xsync (not 'Planned')."""
    xhelp = docs["commands/xhelp.md"]
    available_idx = xhelp.find("### Available now")
    planned_idx = xhelp.find("### Planned")
    assert available_idx != -1, "xhelp.md missing 'Available now' section"
    assert planned_idx != -1, "xhelp.md missing 'Planned' section"
    available_section = xhelp[available_idx:planned_idx]
    assert "/xsync" in available_section, "/xsync should be in Available now, not Planned"


def test_xhelp_lists_xextract_in_available_table(docs):
    """xhelp.md's 'Available now' table must list /xextract."""
    xhelp = docs["commands/xhelp.md"]
    available_idx = xhelp.find("### Available now")
    planned_idx = xhelp.find("### Planned")
    available_section = xhelp[available_idx:planned_idx]
    assert "/xextract" in available_section, "/xextract should be in Available now"


def test_changelog_has_v0_5_0_0_entry(docs):
    """CHANGELOG must have a v0.5.0.0 entry for Slice 4."""
    assert "[0.5.0.0]" in docs["CHANGELOG.md"]


def test_changelog_has_v0_6_0_0_entry(docs):
    """CHANGELOG must have a v0.6.0.0 entry for Slice 5 (scheduled cron)."""
    assert "[0.6.0.0]" in docs["CHANGELOG.md"]


def test_troubleshooting_covers_slice_5_codes(docs):
    """All Slice 5 cron error codes must be documented in TROUBLESHOOTING."""
    troubleshooting = docs["TROUBLESHOOTING.md"]
    for code in [
        "COST_LIMIT_REACHED",
        "SYNC_PUSH_REJECTED",
        "CRON_CONFLICT_UNRESOLVED",
        "SYNC_AUTH_FAILED",
        "EXTRACTION_BACKLOG_GROWING",
    ]:
        assert code in troubleshooting, f"TROUBLESHOOTING.md missing entry for {code}"


def test_changelog_has_entry_for_current_version(docs):
    """CHANGELOG must have an entry matching the active VERSION.

    Original guard (pre-public-README rewrite, commit 03ac0c4) checked the
    README's `**Current slice:**` line. The public README rewrite stripped
    internal slice/version vocabulary and points readers at CHANGELOG.md
    for release detail. The contract being guarded — *"VERSION bumped
    without a corresponding doc entry"* — applies cleanly to CHANGELOG,
    which is the actual public source of truth for releases.
    """
    version = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    changelog = docs["CHANGELOG.md"]
    assert f"[{version}]" in changelog, (
        f"CHANGELOG.md is missing an entry for the current VERSION ({version}). "
        f"Bumping VERSION without adding a CHANGELOG section is the regression "
        f"this test catches."
    )


def test_changelog_has_v0_7_0_0_entry(docs):
    """CHANGELOG must have a v0.7.0.0 entry for Slice 6."""
    assert "[0.7.0.0]" in docs["CHANGELOG.md"]


def test_changelog_has_v0_8_0_0_entry(docs):
    """CHANGELOG must have a v0.8.0.0 entry for Slice 7 (nonce/handshake).

    Slice 7.5 backfill: Slice 7 should have added this test but didn't.
    """
    assert "[0.8.0.0]" in docs["CHANGELOG.md"]


def test_changelog_has_v0_9_0_0_entry(docs):
    """CHANGELOG must have a v0.9.0.0 entry for Slice 7.5 (/xdelete + permissions.ask)."""
    assert "[0.9.0.0]" in docs["CHANGELOG.md"]


def test_changelog_has_v0_9_1_0_entry(docs):
    """CHANGELOG must have a v0.9.1.0 entry for Slice 7.5.1 (user_confirmed
    shim removal) AND must mention BOTH the kwarg it removed AND the
    removal context (removed/TypeError/shim) — so a future copy-paste
    regression that mentions user_confirmed for an unrelated reason still
    fails this gate.
    """
    changelog = docs["CHANGELOG.md"]
    assert "[0.9.1.0]" in changelog, "v0.9.1.0 must have a CHANGELOG entry"
    # Locate the v0.9.1.0 section and assert it references user_confirmed
    start = changelog.find("[0.9.1.0]")
    # Next version header (or end-of-file) bounds the section
    next_header = changelog.find("\n## [", start + 1)
    section = changelog[start : next_header if next_header != -1 else None]
    assert "user_confirmed" in section, (
        "v0.9.1.0 entry should reference the user_confirmed kwarg it removed"
    )
    # Strengthened assertion (per /review): require explicit removal context,
    # not just any reference to user_confirmed.
    removal_tokens = ("removed", "Removed", "TypeError", "shim")
    assert any(tok in section for tok in removal_tokens), (
        "v0.9.1.0 entry must reference removal context "
        f"(one of {removal_tokens}), not just the kwarg name"
    )


def test_troubleshooting_covers_slice_6_codes(docs):
    """All Slice 6 error codes must be documented in TROUBLESHOOTING."""
    troubleshooting = docs["TROUBLESHOOTING.md"]
    for code in [
        "TOMBSTONE_BLOCKED",
        "NO_ROLLBACK_JOURNAL",
        "SETUP_GH_AUTH_REQUIRED",
        "SETUP_DEPLOY_KEY_REJECTED",
        "SETUP_FIRST_RUN_FAILED",
    ]:
        assert code in troubleshooting, f"TROUBLESHOOTING.md missing entry for {code}"


def test_troubleshooting_covers_slice_7_codes(docs):
    """All Slice 7 nonce envelopes must be documented in TROUBLESHOOTING.

    Slice 7.5 backfill: Slice 7 should have added this test.
    """
    troubleshooting = docs["TROUBLESHOOTING.md"]
    for code in [
        "NONCE_REQUIRED",
        "NONCE_INVALID",
        "NONCE_EXPIRED",
        "NONCE_OPERATION_MISMATCH",
        "NONCE_ALREADY_REDEEMED",
    ]:
        assert code in troubleshooting, f"TROUBLESHOOTING.md missing entry for {code}"


def test_troubleshooting_covers_slice_7_5_codes(docs):
    """Slice 7.5 install-time envelopes must be in TROUBLESHOOTING."""
    troubleshooting = docs["TROUBLESHOOTING.md"]
    for code in [
        "SETTINGS_MALFORMED",
        "PERMISSIONS_WILDCARD_OVERRIDE",
    ]:
        assert code in troubleshooting, f"TROUBLESHOOTING.md missing entry for {code}"


def test_xdelete_documented_everywhere(docs):
    """`/xdelete` is the new Slice 7.5 slash command. Must appear in every
    user-facing doc surface (README, CLAUDE.md, TROUBLESHOOTING, CHANGELOG,
    xhelp.md).
    """
    for name, content in docs.items():
        assert "/xdelete" in content, f"{name} doesn't mention /xdelete"


def test_inline_overrides_documented_in_xhelp(docs):
    """All Slice 4 inline overrides (no flags!) must be documented."""
    xhelp = docs["commands/xhelp.md"]
    for keyword in ["inline", "defer", "commit", "proceed dirty", "preview"]:
        assert keyword in xhelp, f"xhelp.md missing '/xsync' override keyword: {keyword}"


def test_oauth_setup_documented(docs):
    """The OAuth setup flow must be discoverable from xhelp + README."""
    xhelp = docs["commands/xhelp.md"]
    readme = docs["README.md"]
    assert "setup_oauth" in xhelp
    assert "setup_oauth" in readme
    assert "developer.x.com" in xhelp
    assert "console.x.com" in xhelp


def test_command_inventory_matches_filesystem():
    """Every commands/*.md file must be referenced in xhelp.md.

    Catches the case where a new slash command lands on disk but xhelp.md
    isn't updated. install_commands.sh is data-driven (D-7), but xhelp.md
    is hand-maintained — this test guards against drift.
    """
    commands_dir = PROJECT_ROOT / "commands"
    xhelp_content = (commands_dir / "xhelp.md").read_text(encoding="utf-8")
    for md_file in commands_dir.glob("*.md"):
        cmd_name = "/" + md_file.stem
        if cmd_name == "/xhelp":
            continue  # xhelp documents itself
        assert cmd_name in xhelp_content, (
            f"commands/{md_file.name} exists but {cmd_name} is not in xhelp.md"
        )


def test_troubleshooting_covers_new_oauth_codes(docs):
    """All Slice 4 OAUTH_* error codes must be documented in TROUBLESHOOTING."""
    troubleshooting = docs["TROUBLESHOOTING.md"]
    for code in [
        "OAUTH_SETUP_REQUIRED",
        "OAUTH_PORT_COLLISION",
        "OAUTH_BROWSER_NOT_DEFAULT",
        "OAUTH_GRANT_REFUSED",
        "OAUTH_KEYCHAIN_BLOCKED",
        "X_API_RATE_LIMITED",
        "X_API_NETWORK_ERROR",
    ]:
        assert code in troubleshooting, f"TROUBLESHOOTING.md missing entry for {code}"
