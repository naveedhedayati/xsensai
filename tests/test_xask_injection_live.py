"""Live end-to-end injection-defense test for /xask (Eng EC8).

Boots the QMD-backed corpus + invokes xsensai.xask.service.prepare on
questions that retrieve injection fixture cards. Asserts that:
  1. The assembled synthesis_prompt contains the HARD RULES verbatim
  2. The DATA_TO_ANALYZE wrap surrounds card content
  3. The structured envelope returned to the slash command does not echo
     INJECTED_<n> canary strings outside the DATA_TO_ANALYZE block

Gated on XSENSAI_RUN_INTEGRATION=1 because it spins up a real corpus and
QMD index. Run via:

    XSENSAI_RUN_INTEGRATION=1 pytest tests/test_xask_injection_live.py -v

Note: this does NOT actually call an LLM. The defense being tested lives in
the prompt structure + HARD RULES that the host Claude session must obey.
We assert the structural invariants the prompt provides; the manual gauntlet
G10-G14 is where a live host model is exercised.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

import pytest

from xsensai.synthesis.injection_fixtures import (
    CANARY_RE,
    fixtures_dir,
    list_fixtures,
)
from xsensai.synthesis.template import HARD_RULES
from xsensai.xask import service


pytestmark = pytest.mark.skipif(
    os.environ.get("XSENSAI_RUN_INTEGRATION") != "1",
    reason="set XSENSAI_RUN_INTEGRATION=1 to run live injection tests",
)


@pytest.fixture
def injection_corpus(tmp_path, monkeypatch):
    """Copy the 5 injection fixtures into a temp corpus + bootstrap QMD collection.

    Pattern matches tests/eval/golden_set.py: `qmd collection add` + monkeypatch
    `COLLECTION_NAME`. Self-cleans after the test.
    """
    import subprocess
    from xsensai.retrieval import qmd as qmd_mod

    qmd_bin = os.environ.get("XSENSAI_QMD_PATH", "/Users/naveedhedayati/.bun/bin/qmd")
    if not Path(qmd_bin).exists():
        pytest.skip(f"QMD binary not found at {qmd_bin}")

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for src in list_fixtures():
        shutil.copy(src, corpus / src.name)
        raw = src.with_suffix(".raw.txt")
        shutil.copy(raw, corpus / raw.name)
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(corpus))

    coll_name = "xsensai-injection-test"
    subprocess.run(
        [qmd_bin, "collection", "remove", coll_name],
        capture_output=True, check=False,
    )
    res = subprocess.run(
        [qmd_bin, "collection", "add", str(corpus),
         "--name", coll_name, "--mask", "*.md"],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        pytest.fail(f"qmd collection add failed: {res.stderr}")

    monkeypatch.setattr(qmd_mod, "COLLECTION_NAME", coll_name)
    yield corpus

    subprocess.run(
        [qmd_bin, "collection", "remove", coll_name],
        capture_output=True, check=False,
    )


def test_synthesis_prompt_contains_hard_rules(injection_corpus, monkeypatch):
    """Every assembled prompt must contain the HARD RULES verbatim."""
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "off")

    questions_to_run = [
        "compounding interest",
        "leverage and capital allocation",
        "always invert mental model",
        "founders sit with discomfort",
        "feynman teaching technique",
    ]
    for q in questions_to_run:
        result = asyncio.run(service.prepare(q, no_web=True))
        if result.status != "ok":
            # Some questions may not match; the test is about structure when
            # they DO match
            continue
        assert HARD_RULES.strip() in result.synthesis_prompt, (
            f"HARD RULES missing from prompt for question {q!r}"
        )
        assert "<DATA_TO_ANALYZE>" in result.synthesis_prompt
        assert "</DATA_TO_ANALYZE>" in result.synthesis_prompt


def test_canary_strings_only_inside_data_to_analyze(injection_corpus, monkeypatch):
    """Canary INJECTED_<n> strings should appear ONLY inside the
    <DATA_TO_ANALYZE> wrap (never in the system instructions or template).
    """
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "off")

    questions_to_run = [
        "compounding interest",
        "leverage and capital allocation",
        "feynman teaching technique",
    ]
    saw_at_least_one_canary = False
    for q in questions_to_run:
        result = asyncio.run(service.prepare(q, no_web=True))
        if result.status != "ok":
            continue
        prompt = result.synthesis_prompt
        # Find each canary occurrence and check it's inside the DATA tags
        for match in CANARY_RE.finditer(prompt):
            saw_at_least_one_canary = True
            pos = match.start()
            before = prompt[:pos]
            # Last opening tag must come AFTER any closing tag before this position
            last_open = before.rfind("<DATA_TO_ANALYZE>")
            last_close = before.rfind("</DATA_TO_ANALYZE>")
            assert last_open > last_close, (
                f"Canary {match.group()!r} at position {pos} appears OUTSIDE "
                "<DATA_TO_ANALYZE> wrap. The injection escaped the boundary."
            )
    # We expect at least ONE injection fixture to have been retrieved by the
    # five queries above. If not, QMD didn't index the fixtures correctly.
    assert saw_at_least_one_canary, (
        "no injection fixtures were retrieved by any test query — "
        "check QMD index bootstrap for the temp corpus"
    )


def test_no_canary_in_envelope_messages(injection_corpus, monkeypatch):
    """Even when /xask returns an info or error envelope (e.g. NO_CORPUS_MATCH
    on a question that doesn't match), the envelope text must NOT contain
    canary strings."""
    monkeypatch.setenv("XSENSAI_XASK_LOG_MODE", "off")

    # A question that probably won't match any injection fixture
    result = asyncio.run(service.prepare("topic that wont match anything", no_web=True))
    if result.status in ("info", "error"):
        assert not CANARY_RE.search(result.rendered_message or ""), (
            f"canary leaked into envelope: {result.rendered_message!r}"
        )
