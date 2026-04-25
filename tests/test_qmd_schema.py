"""Contract test: tests/fixtures/qmd_query_output.json matches our parser shape.

This locks the QMD JSON output schema we observed during the Slice 1 spike.
If QMD changes its output, this test fails loudly so we can update qmd.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from xsensai.retrieval import qmd


def test_fixture_parses_into_qmd_hits(fixtures_dir: Path) -> None:
    fixture = fixtures_dir / "qmd_query_output.json"
    raw = fixture.read_bytes()
    hits = qmd._parse_qmd_json(raw)
    assert len(hits) >= 1
    h = hits[0]
    assert isinstance(h, qmd.QMDHit)
    assert h.docid.startswith("#")
    assert isinstance(h.score, float)
    assert h.file_uri.startswith("qmd://")
    assert h.title  # non-empty
    assert h.snippet  # non-empty


def test_fixture_keys_are_what_we_expect(fixtures_dir: Path) -> None:
    """If QMD adds/removes/renames fields, this is the canary."""
    fixture = fixtures_dir / "qmd_query_output.json"
    data = json.loads(fixture.read_text())
    expected_keys = {"docid", "score", "file", "title", "snippet"}
    for item in data:
        actual = set(item.keys())
        # We require AT LEAST the expected keys (extras are tolerated)
        assert expected_keys.issubset(actual), (
            f"QMD output schema changed. Expected at least {expected_keys}, got {actual}. "
            "Update tests/fixtures/qmd_query_output.json + xsensai.retrieval.qmd parser."
        )


def test_resolve_path_strips_qmd_prefix(tmp_path: Path) -> None:
    h = qmd.QMDHit(
        docid="#abc",
        score=0.9,
        file_uri=f"qmd://{qmd.COLLECTION_NAME}/2026-04-20-paulg.md",
        title="Why startups",
        snippet="...",
    )
    resolved = h.resolve_path(tmp_path)
    assert resolved == tmp_path / "2026-04-20-paulg.md"
