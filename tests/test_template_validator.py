"""Tests for xsensai.synthesis.template.validate."""

from __future__ import annotations

import pytest

from xsensai.synthesis.template import (
    GROUNDEDNESS_MIN_DISTINCT_CARDS,
    OUTPUT_TEMPLATE,
    groundedness_check,
    validate,
)


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


# ----- References bullet reconciliation -------------------------------------
# The validator (AD-E7 / _REFERENCE_LINE_RE) only counts BULLETED `[B]/[P]`
# lines. The prompt-side instruction must therefore tell the host to bullet
# its references — otherwise a faithful host emits non-bulleted lines and
# validate() fails on call #1. These guard the prompt side so it can't drift
# back to the old "use the format_reference() format" (no-bullet) wording.


def test_output_template_requires_bulleted_references():
    """OUTPUT_TEMPLATE must instruct one bulleted `- [B]/[P]` line per card."""
    refs_block = OUTPUT_TEMPLATE.split("## References", 1)[1]
    assert "- " in refs_block
    assert "[B]" in refs_block and "[P]" in refs_block
    # The instruction must be explicit that the bullet is mandatory, so the
    # host doesn't treat it as cosmetic.
    assert "REQUIRED" in refs_block or "bullet" in refs_block.lower()


def test_stricter_reprompt_mentions_bulleted_references():
    """The retry guidance must name the bullet requirement so call #2 recovers."""
    res = validate("## From your corpus\nfoo\n", web_attempted=False)
    msg = res.stricter_reprompt()
    assert "bullet" in msg.lower()
    assert "- " in msg


def test_format_reference_line_passes_validator_when_bulleted():
    """A reference line in format_reference()'s layout validates once bulleted.

    Ties the formatter's output shape to the validator: format_reference()
    itself stays bullet-free (it feeds the input-context `reference:` lines, not
    the user-facing References block), and prepending '- ' is exactly what the
    prompt now asks the host to do — that bulleted line must validate.
    """
    from datetime import datetime, timezone
    from pathlib import Path

    from xsensai.model.card import CardFrontmatter, LoadedCard
    from xsensai.retrieval.format import format_reference

    cf = CardFrontmatter(
        source_type="bookmark",
        source="https://x.com/paulg/status/123",
        source_id="123",
        author="@paulg",
        captured=datetime(2026, 4, 20, tzinfo=timezone.utc),
        why_saved="explained well",
        raw_path="./x.raw.txt",
        raw_checksum="sha256:" + "0" * 64,
    )
    card = LoadedCard(
        fm=cf,
        body="## Content\n\nMost great startups began as side projects.",
        raw_bytes=b"",
        md_path=Path("card.md"),
    )
    ref = format_reference(card)
    assert not ref.startswith("- "), "format_reference itself stays bullet-free"
    draft = "\n".join([
        "## From your corpus",
        "body",
        "",
        "## Synthesis",
        "syn",
        "",
        "## References",
        f"- {ref}",
        "",
    ])
    res = validate(draft, web_attempted=False)
    assert res.valid, res.reasons


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


# ----- CV-6: groundedness (cite-or-abstain, >=2 distinct cards) -------------
# groundedness_check() is a SEPARATE corroboration gate, not the structural
# floor. Structural only (no claim/card text comparison — EC10). Distinct cards
# are counted against the RETURNED ids (AD-E7), not parsed from rendered refs.

# Returned-id fixtures: a bookmark id ends in its numeric source_id (which the
# rendered permalink carries) and a paste id IS its filename stem.
_CARD_A = "2026-04-20-paulg-1234567890"           # bookmark
_CARD_B = "paste-2026-04-18-cofounder-meeting-notes"  # paste
_REF_A = "- [B] @paulg — startups | https://x.com/paulg/status/1234567890 | why: x"
_REF_B = "- [P] notes — cofounder fit | paste-2026-04-18-cofounder-meeting-notes.md | why: y"


def _g_draft(synthesis: str, *refs: str, corpus: str = "Grounded take.") -> str:
    parts = ["## From your corpus", corpus, "", "## Synthesis", synthesis, "",
             "## References"]
    parts.extend(refs)
    return "\n".join(parts)


def test_groundedness_two_distinct_returned_cards_is_grounded():
    draft = _g_draft(
        "Cofounder fit matters [B], and startups begin as side projects [P].",
        _REF_A, _REF_B,
    )
    res = groundedness_check(draft, candidate_card_ids=[_CARD_A, _CARD_B])
    assert res.grounded, res.reasons
    assert res.abstained is False
    assert res.distinct_cards == 2
    assert res.unsupported_claims == []


def test_groundedness_single_distinct_card_not_grounded():
    """One cited returned card passes the structural floor but fails the >=2 bar."""
    draft = _g_draft("Startups begin as side projects [B].", _REF_A)
    res = groundedness_check(draft, candidate_card_ids=[_CARD_A, _CARD_B])
    assert res.distinct_cards == 1  # only _CARD_A's source_id appears
    assert res.grounded is False
    assert any("distinct" in r for r in res.reasons)
    # ...and the structural validator still accepts it (floor unchanged).
    assert validate(draft, web_attempted=False).valid


def test_groundedness_distinct_counted_against_returned_ids_not_refs():
    """AD-E7: a rendered ref to a card the tool did NOT return must not inflate
    the distinct count. Only ids in candidate_card_ids can count."""
    draft = _g_draft(
        "Claim one [B] and claim two [P].",
        _REF_A,
        "- [P] rogue — not returned | rogue-card-not-returned.md | why: z",
    )
    res = groundedness_check(draft, candidate_card_ids=[_CARD_A])  # rogue not returned
    assert res.distinct_cards == 1
    assert res.grounded is False


def test_groundedness_same_card_twice_counts_once():
    """Two reference lines, same returned card -> one distinct -> not grounded.
    Guards faking corroboration by restating one source."""
    draft = _g_draft(
        "Point [B] and the same point again [B].",
        _REF_A,
        "- [B] @paulg — restated | https://x.com/paulg/status/1234567890 | why: z",
    )
    res = groundedness_check(draft, candidate_card_ids=[_CARD_A, _CARD_B])
    assert res.distinct_cards == 1
    assert res.grounded is False


def test_groundedness_uncited_synthesis_line_flagged():
    """§4.3(a): a Synthesis line with no [B]/[P] and no hedge is unsupported."""
    draft = _g_draft(
        "Founders should obsess over cofounder fit.",  # no citation, no hedge
        _REF_A, _REF_B,
    )
    res = groundedness_check(draft, candidate_card_ids=[_CARD_A, _CARD_B])
    assert res.unsupported_claims  # the bare line
    assert res.grounded is False
    assert any("Synthesis" in r for r in res.reasons)


def test_groundedness_hedge_satisfies_claim_support():
    """§4.3(a): the explicit hedge counts as support for an uncited line."""
    draft = _g_draft(
        "Founders value cofounder fit [B]; markets may shift "
        "(no corpus support — general knowledge).",
        _REF_A, _REF_B,
    )
    res = groundedness_check(draft, candidate_card_ids=[_CARD_A, _CARD_B])
    assert res.unsupported_claims == []
    assert res.grounded is True


def test_groundedness_abstention_is_grounded_without_two_cards():
    draft = _g_draft(
        "No grounded claim to make.",
        corpus="Your corpus doesn't cover this question.",
    )
    res = groundedness_check(draft, candidate_card_ids=[])
    assert res.abstained is True
    assert res.grounded is True


def test_groundedness_abstention_phrase_no_relevant_cards():
    draft = _g_draft(
        "Nothing to synthesize.",
        _REF_A,
        corpus="There are no relevant cards in your corpus for this.",
    )
    res = groundedness_check(draft, candidate_card_ids=[_CARD_A])
    assert res.abstained is True
    assert res.grounded is True


def test_groundedness_ordinary_doesnt_is_not_abstention():
    """A normal answer that merely contains 'doesn't' is NOT an abstention,
    so the >=2 distinct-card bar still applies."""
    draft = _g_draft(
        "Wealth doesn't come from selling time; it compounds via leverage [B].",
        _REF_A,
    )
    res = groundedness_check(draft, candidate_card_ids=[_CARD_A, _CARD_B])
    assert res.abstained is False
    assert res.distinct_cards == 1
    assert res.grounded is False


def test_groundedness_substring_collision_does_not_inflate_distinct():
    """Regression: a shorter source_id (456) must NOT match inside a longer
    one (123456). Only the genuinely-cited card counts."""
    cand = ["2026-04-20-a-456", "2026-04-20-b-123456"]
    draft = _g_draft(
        "Only card b is cited here [B].",
        "- [B] @b — point | https://x.com/b/status/123456 | why: y",
    )
    res = groundedness_check(draft, candidate_card_ids=cand)
    assert res.distinct_cards == 1, "456 must not match inside 123456"
    assert res.grounded is False


def test_groundedness_id_in_corpus_prose_not_counted():
    """Regression: an id mentioned outside `## References` (here, in the corpus
    prose) does not count as a citation — only the References block does."""
    draft = _g_draft(
        "A claim [B].",
        _REF_A,  # only card A is in References
        corpus=f"Grounded take; see also {_CARD_B}.md elsewhere.",
    )
    res = groundedness_check(draft, candidate_card_ids=[_CARD_A, _CARD_B])
    assert res.distinct_cards == 1, "card B is only in prose, not References"
    assert res.grounded is False


def test_groundedness_empty_candidate_ids_non_abstain_fails():
    """No returned ids + a non-abstaining claim -> 0 distinct -> not grounded."""
    draft = _g_draft("A bold claim [B].", _REF_A)
    res = groundedness_check(draft, candidate_card_ids=[])
    assert res.distinct_cards == 0
    assert res.grounded is False
    assert any("distinct" in r for r in res.reasons)


def test_groundedness_falsy_candidate_id_skipped():
    """A falsy ("" / None-ish) entry in the candidate list is skipped cleanly."""
    res_with = groundedness_check(
        _g_draft("A claim [B].", _REF_A), candidate_card_ids=["", _CARD_A]
    )
    res_without = groundedness_check(
        _g_draft("A claim [B].", _REF_A), candidate_card_ids=[_CARD_A]
    )
    assert res_with.distinct_cards == res_without.distinct_cards == 1


def test_groundedness_min_distinct_constant_is_two():
    assert GROUNDEDNESS_MIN_DISTINCT_CARDS == 2
