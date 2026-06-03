"""Fork-hygiene guard (REPO_READINESS_PLAN P0-6).

Asserts no author-specific path/slug leaks into the *tracked* tree, so a fork
never runs against the author's machine or repo. Scoped via `git ls-files`, so
untracked working files (the plan docs, build artifacts, dev-notes/) are out of
scope by construction.

Token set is intentionally path/slug-shaped — NOT a bare "Naveed" — so
legitimate MIT authorship (`pyproject.toml`) and historical CHANGELOG links are
preserved.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent

# Author-machine / author-repo leak tokens. Path/slug shaped on purpose.
LEAK_TOKENS = (
    "naveedhedayati/",          # GitHub repo slug
    "/Users/naveed",            # absolute home path
    "me@naveed",                # author email
    "Documents/Vault",          # author vault path
    ".bun/bin/qmd",             # author-specific qmd binary path
)

# Legitimate, documented exceptions:
#   - CHANGELOG.md: historical release / action-run links.
#   - pyproject.toml: MIT authorship + the canonical upstream repo URL.
#   - this test: it necessarily contains the tokens as string literals.
ALLOWLIST = {
    "CHANGELOG.md",
    "pyproject.toml",
    "tests/test_fork_hygiene.py",
}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def test_no_author_strings_in_tracked_tree():
    violations: list[str] = []
    for rel in _tracked_files():
        if rel in ALLOWLIST:
            continue
        path = PROJECT_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeError):
            continue
        for token in LEAK_TOKENS:
            if token in text:
                violations.append(f"{rel}: contains {token!r}")

    assert not violations, (
        "Author-specific path/slug leaked into the tracked tree "
        "(a fork would run against the author's machine/repo):\n  "
        + "\n  ".join(violations)
    )
