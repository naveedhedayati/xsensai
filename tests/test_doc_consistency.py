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


def test_readme_current_slice_is_4(docs):
    """README's 'Current slice' line must reference Slice 4."""
    readme = docs["README.md"]
    # Find the current-slice line
    current_line = None
    for line in readme.splitlines():
        if "**Current slice:**" in line:
            current_line = line
            break
    assert current_line is not None, "README missing **Current slice:** line"
    assert "Slice 4" in current_line, f"README current slice not Slice 4: {current_line}"


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
