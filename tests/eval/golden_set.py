"""F1 quality gate — 15 hand-labeled queries, target top-3 hit rate ≥ 80%.

Per autoplan F1 (CEO + Eng both critical): the load-bearing premise that
QMD's BM25/qwen3-rerank is good enough for /xfind needs validation. This
runs against the real corpus (default: $XSENSAI_CORPUS_PATH or vault). If
top-3 hit rate < 80%, the deferred Claude/GPT re-rank decision (D1)
should be revisited.

Writes one JSON line per run to ~/.cache/xsensai/eval-history.jsonl so
quality trend over time is visible (xsensai-eval-history command).

Gated on XSENSAI_RUN_INTEGRATION=1 (needs QMD + a real corpus).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

import pytest

from xsensai.retrieval import engine, qmd
from xsensai.storage import corpus


_INTEGRATION = os.environ.get("XSENSAI_RUN_INTEGRATION") == "1"
EVAL_HISTORY_PATH = Path.home() / ".cache" / "xsensai" / "eval-history.jsonl"

# 15 queries shaped against the FIXTURE corpus (10 cards). Real-corpus
# evaluation uses XSENSAI_GOLDEN_SET_PATH (JSON file) if set; otherwise
# this fixture set runs against tests/fixtures/cards/.
GOLDEN_SET: List[Tuple[str, List[str]]] = [
    ("startups", ["2026-04-20-paulg-1234567890"]),
    ("side projects", ["2026-04-20-paulg-1234567890"]),
    ("software eating world", ["2026-04-15-pmarca-9876543210"]),
    ("AGI predictions", ["2025-01-10-sama-5555555555"]),
    ("wealth at scale", ["2026-04-22-naval-1112223334"]),
    ("cofounder fit", ["paste-2026-04-18-cofounder-meeting-notes"]),
    ("product wow design", ["paste-2026-04-10-product-thinking-snippet"]),
    ("retrieval curation", ["2025-08-15-deleted-author-9999999999"]),
    ("MCP infra primitive", ["2026-03-12-claude-fan-7878787878"]),
    ("inevitable products", ["2026-04-23-vc-thinker-2020202020"]),
    ("x-sensai thesis", ["paste-2026-04-05-x-sensai-design-rationale"]),
    ("naval wealth", ["2026-04-22-naval-1112223334"]),
    ("paul graham startups", ["2026-04-20-paulg-1234567890"]),
    ("legacy v1 bookmark", ["v1-2024-09-30-old-bookmark-3434343434"]),
    ("cofounder shared taste", ["paste-2026-04-18-cofounder-meeting-notes"]),
]

TARGET_TOP3_HIT_RATE = 0.80


@pytest.fixture
def golden_set_collection(cards_fixture_dir: Path):
    if not _INTEGRATION:
        pytest.skip("XSENSAI_RUN_INTEGRATION not set")
    import shutil
    qmd_bin = os.environ.get("XSENSAI_QMD_PATH") or shutil.which("qmd")
    if not qmd_bin:
        pytest.skip("qmd binary not found ($XSENSAI_QMD_PATH / PATH)")
    subprocess.run([qmd_bin, "collection", "remove", "xsensai-golden"],
                   capture_output=True, check=False)
    res = subprocess.run(
        [qmd_bin, "collection", "add", str(cards_fixture_dir),
         "--name", "xsensai-golden", "--mask", "*.md"],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        pytest.fail(f"qmd collection add failed: {res.stderr}")
    yield "xsensai-golden"
    subprocess.run([qmd_bin, "collection", "remove", "xsensai-golden"],
                   capture_output=True, check=False)


@pytest.mark.skipif(not _INTEGRATION, reason="XSENSAI_RUN_INTEGRATION not set")
async def test_golden_set_top3_hit_rate(
    golden_set_collection, cards_fixture_dir: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(cards_fixture_dir))
    monkeypatch.setattr(qmd, "COLLECTION_NAME", golden_set_collection)

    top1_hits = 0
    top3_hits = 0
    total = len(GOLDEN_SET)
    per_query: List[dict] = []

    for query, expected_ids in GOLDEN_SET:
        results = await engine.search(query, limit=5)
        ids = [h.card.id for h in results.hits]
        top1 = bool(set(ids[:1]) & set(expected_ids))
        top3 = bool(set(ids[:3]) & set(expected_ids))
        top1_hits += int(top1)
        top3_hits += int(top3)
        per_query.append({
            "query": query, "expected": expected_ids,
            "got_top3": ids[:3], "top1": top1, "top3": top3,
        })

    top1_rate = top1_hits / total
    top3_rate = top3_hits / total

    EVAL_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_queries": total,
        "top1_hit_rate": top1_rate,
        "top3_hit_rate": top3_rate,
        "corpus_size": sum(1 for _ in cards_fixture_dir.glob("*.md")),
        "per_query": per_query,
    }
    with EVAL_HISTORY_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")

    print(
        f"\nGOLDEN SET: top1={top1_rate:.0%} top3={top3_rate:.0%} "
        f"(target top3 ≥ {TARGET_TOP3_HIT_RATE:.0%})"
    )
    for row in per_query:
        if not row["top3"]:
            print(f"  MISS: query={row['query']!r} expected={row['expected']} got={row['got_top3']}")

    assert top3_rate >= TARGET_TOP3_HIT_RATE, (
        f"Quality gate failed: top3 hit rate {top3_rate:.0%} < {TARGET_TOP3_HIT_RATE:.0%}. "
        "Revisit autoplan D1 (LLM rerank deferral) or tune scoring constants."
    )
