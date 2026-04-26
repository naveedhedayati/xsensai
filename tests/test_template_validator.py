"""Tests for xsensai.synthesis.template.validate."""

from __future__ import annotations

import pytest

from xsensai.synthesis.template import validate


def _draft_full(
    *, with_tension: bool = False, with_web: bool = False, web_unavail: bool = False
) -> str:
    parts = ["## From your corpus", "Some grounded take.", ""]
    if with_tension:
        parts.extend(["## Internal tension", "There is a dissenter.", ""])
    if with_web:
        parts.extend(["## Web this week", "Fresh web context.", ""])
    if web_unavail:
        parts.extend(["## (web context unavailable this run — timeout)", ""])
    parts.extend([
        "## Synthesis",
        "Three line max synthesis section",
        "",
        "## References",
        "- [B] @author — snippet | url | why: x",
        "",
    ])
    return "\n".join(parts)


def test_valid_minimal_no_web():
    draft = _draft_full(with_web=False)
    res = validate(draft, web_attempted=False)
    assert res.valid, res.reasons


def test_valid_with_web_in_time():
    draft = _draft_full(with_web=True)
    res = validate(draft, web_attempted=True)
    assert res.valid, res.reasons


def test_valid_with_web_unavailable():
    draft = _draft_full(web_unavail=True)
    res = validate(draft, web_attempted=True)
    assert res.valid, res.reasons


def test_valid_with_tension():
    draft = _draft_full(with_tension=True, with_web=True)
    res = validate(
        draft,
        web_attempted=True,
        challenge_used=True,
        challenge_found_dissenter=True,
    )
    assert res.valid, res.reasons


def test_invalid_missing_corpus_section():
    draft = "## Synthesis\nfoo\n## References\n- [B] x\n"
    res = validate(draft, web_attempted=False)
    assert not res.valid
    assert any("From your corpus" in r for r in res.reasons)


def test_invalid_missing_synthesis_section():
    draft = "## From your corpus\nfoo\n## References\n- [B] x\n"
    res = validate(draft, web_attempted=False)
    assert not res.valid
    assert any("Synthesis" in r for r in res.reasons)


def test_invalid_missing_references_section():
    draft = "## From your corpus\nfoo\n## Synthesis\nbar\n"
    res = validate(draft, web_attempted=False)
    assert not res.valid
    assert any("References" in r for r in res.reasons)


def test_invalid_tension_without_dissenter():
    draft = _draft_full(with_tension=True)
    res = validate(draft, web_attempted=False, challenge_used=False)
    assert not res.valid
    assert any("Internal tension" in r for r in res.reasons)


def test_invalid_dissenter_without_tension():
    draft = _draft_full(with_tension=False)
    res = validate(
        draft,
        web_attempted=False,
        challenge_used=True,
        challenge_found_dissenter=True,
    )
    assert not res.valid
    assert any("dissenter" in r for r in res.reasons)


def test_invalid_both_web_sections():
    draft = _draft_full(with_web=True, web_unavail=True)
    res = validate(draft, web_attempted=True)
    assert not res.valid
    assert any("both" in r.lower() for r in res.reasons)


def test_invalid_no_web_section_when_attempted():
    draft = _draft_full(with_web=False, web_unavail=False)
    res = validate(draft, web_attempted=True)
    assert not res.valid


def test_invalid_web_section_when_no_web():
    draft = _draft_full(with_web=True)
    res = validate(draft, web_attempted=False)
    assert not res.valid
    assert any("no web" in r.lower() for r in res.reasons)


def test_stricter_reprompt_useful_when_invalid():
    draft = "## From your corpus\nfoo\n"
    res = validate(draft, web_attempted=False)
    msg = res.stricter_reprompt()
    assert "EXACTLY" in msg
    assert "## Synthesis" in msg
    assert "## References" in msg


def test_stricter_reprompt_empty_when_valid():
    res = validate(_draft_full(), web_attempted=False)
    assert res.stricter_reprompt() == ""


# ----- F7: section ordering -------------------------------------------------


def test_invalid_synthesis_before_references_swapped():
    """Sections in the wrong order fail validation (F7 fix)."""
    draft = "\n".join([
        "## From your corpus",
        "corpus body",
        "",
        "## References",
        "- [B] @x — y | z | why: a",
        "",
        "## Synthesis",
        "wrong order",
        "",
    ])
    res = validate(draft, web_attempted=False)
    assert not res.valid
    assert any("ordering" in r.lower() for r in res.reasons)


def test_invalid_corpus_after_synthesis():
    """## From your corpus must come first."""
    draft = "\n".join([
        "## Synthesis",
        "first",
        "",
        "## From your corpus",
        "second",
        "",
        "## References",
        "- [B] @x — y | z | why: a",
        "",
    ])
    res = validate(draft, web_attempted=False)
    assert not res.valid
    assert any("ordering" in r.lower() for r in res.reasons)


# ----- F8: References cardinality -------------------------------------------


def test_invalid_zero_references():
    """References section present but with no cited cards is invalid."""
    draft = "\n".join([
        "## From your corpus",
        "body",
        "",
        "## Synthesis",
        "syn",
        "",
        "## References",
        "(none cited)",
        "",
    ])
    res = validate(draft, web_attempted=False)
    assert not res.valid
    assert any("References" in r and "1-3" in r for r in res.reasons)


def test_invalid_four_references():
    """4+ references violates the locked spec cap of 3."""
    draft = "\n".join([
        "## From your corpus",
        "body",
        "",
        "## Synthesis",
        "syn",
        "",
        "## References",
        "- [B] @a — t | u | why: 1",
        "- [B] @b — t | u | why: 2",
        "- [B] @c — t | u | why: 3",
        "- [B] @d — t | u | why: 4",
        "",
    ])
    res = validate(draft, web_attempted=False)
    assert not res.valid
    assert any("4" in r and "caps at 3" in r for r in res.reasons)


def test_valid_three_references():
    """Exactly 3 cited cards is valid (boundary)."""
    draft = "\n".join([
        "## From your corpus",
        "body",
        "",
        "## Synthesis",
        "syn",
        "",
        "## References",
        "- [B] @a — t | u | why: 1",
        "- [P] example.com — t | u | why: 2",
        "- [B] @c — t | u | why: 3",
        "",
    ])
    res = validate(draft, web_attempted=False)
    assert res.valid, res.reasons


# ----- F9: Synthesis line cap -----------------------------------------------


def test_invalid_synthesis_too_many_lines():
    """## Synthesis with 4+ lines violates locked spec cap of 3."""
    draft = "\n".join([
        "## From your corpus",
        "body",
        "",
        "## Synthesis",
        "line one",
        "line two",
        "line three",
        "line four",
        "",
        "## References",
        "- [B] @a — t | u | why: 1",
        "",
    ])
    res = validate(draft, web_attempted=False)
    assert not res.valid
    assert any("Synthesis" in r and "4" in r for r in res.reasons)


def test_valid_synthesis_three_lines_at_cap():
    """Exactly 3 lines is valid (boundary)."""
    draft = "\n".join([
        "## From your corpus",
        "body",
        "",
        "## Synthesis",
        "line one",
        "line two",
        "line three",
        "",
        "## References",
        "- [B] @a — t | u | why: 1",
        "",
    ])
    res = validate(draft, web_attempted=False)
    assert res.valid, res.reasons
