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
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

import pytest

from xsensai.retrieval import engine, qmd
from xsensai.storage import corpus, sidecar


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

# ---------------------------------------------------------------------------
# CV-3: honest eval. The GOLDEN_SET above is keyword-shaped — its queries
# share vocabulary with the target cards ("naval wealth", "paul graham
# startups"), so BM25 alone can win. That flatters the system. Two additions
# stress it the way a real user does:
#
#   1. PARAPHRASE_SET — semantic queries with LOW literal overlap with the
#      target card. retrieval is BM25-only (qmd search; vector search is
#      deliberately OFF — see retrieval/qmd.py), so these expose the BM25
#      SEMANTIC CEILING: a query with no shared terms retrieves nothing. This
#      set is the diagnostic that will move when the deferred LLM/vector
#      rerank (autoplan D1) lands; it is tracked, not hard-gated, because a
#      0.00 here reflects the current architecture, not a regression.
#   2. HARD_NEGATIVES — distractor cards that are topically adjacent but wrong.
#      They must NOT outrank the true target. Injected into the eval corpus
#      (a tmp copy of the fixtures) so the shared tests/fixtures/cards/ dir is
#      untouched. This IS hard-gated (precision must not regress).
#
# MRR (mean reciprocal rank) is reported alongside top-1/top-3 because it is
# rank-sensitive: a system that buries the right card at position 3 scores
# worse than one that nails position 1, which top-3 hit rate cannot see.
#
# Thresholds here are checkpoint-derived starting points (REPO_READINESS_PLAN
# §4 owns the final bar); they are calibrated against an observed run, set
# below the observed value with margin, exactly like TARGET_TOP3_HIT_RATE.

PARAPHRASE_SET: List[Tuple[str, List[str]]] = [
    # query (low literal overlap)                        -> expected card id(s)
    ("when will machines reach human-level intelligence",
     ["2025-01-10-sama-5555555555"]),
    ("code will reshape every sector of the economy",
     ["2026-04-15-pmarca-9876543210"]),
    ("how do you get rich by helping many people",
     ["2026-04-22-naval-1112223334"]),
    ("great tools seem obvious once they already exist",
     ["2026-04-23-vc-thinker-2020202020"]),
    ("the key building block for wiring models to tools",
     ["2026-03-12-claude-fan-7878787878"]),
    ("interfaces that delight a person at first glance",
     ["paste-2026-04-10-product-thinking-snippet"]),
    ("choosing the right business partner over the idea",
     ["paste-2026-04-18-cofounder-meeting-notes"]),
    ("a personal knowledge base steering an assistant instead of the generic internet",
     ["paste-2026-04-05-x-sensai-design-rationale"]),
]

# Topically-adjacent distractors. Each is near a real query's subject but is
# the wrong answer; a precise retriever keeps them out of rank 1.
HARD_NEGATIVES: List[dict] = [
    {
        "stem": "hardneg-ai-regulation",
        "author": "@policy-wonk",
        "summary": "Argues AI regulation will slow the pace of model releases.",
        "tags": ["ai", "regulation", "policy"],
        "body": "New compliance rules will slow how fast labs ship frontier models.",
    },
    {
        "stem": "hardneg-frugality",
        "author": "@thrift-poster",
        "summary": "Personal-finance take on frugality and cutting spending.",
        "tags": ["money", "frugality", "budget"],
        "body": "The fastest path to savings is cutting recurring subscriptions you forgot about.",
    },
    {
        "stem": "hardneg-hardware-comeback",
        "author": "@chip-bull",
        "summary": "Thesis that domestic hardware manufacturing is making a comeback.",
        "tags": ["hardware", "manufacturing", "supply-chain"],
        "body": "Reshoring chip fabs means hardware, not software, is the next decade's story.",
    },
    {
        "stem": "hardneg-first-hire",
        "author": "@hiring-notes",
        "summary": "Notes on making your first engineering hire after founding.",
        "tags": ["hiring", "team", "employees"],
        "body": "Your first hire should de-risk the thing you are personally worst at.",
    },
    {
        "stem": "hardneg-enterprise-sales",
        "author": "@gtm-thinker",
        "summary": "Walkthrough of a multi-stage enterprise sales motion.",
        "tags": ["sales", "enterprise", "gtm"],
        "body": "Enterprise deals close on procurement timelines, not product demos.",
    },
]

# Calibrated 2026-06-03 against a local qmd run (logged to eval-history.jsonl):
#   keyword MRR    = 0.967  (BM25 nails keyword-shaped queries)
#   paraphrase MRR = 0.000  (BM25 retrieves nothing on zero-overlap queries)
#   hard-neg top-1 leaks = 0
# The HARD GATE is on keyword MRR (the system's actual competency), set with
# margin below the observed value. Paraphrase MRR is a tracked DIAGNOSTIC, not
# a gate: 0.00 reflects the BM25-only architecture (vector search off), so
# gating it would fail red on a documented, deferred limitation (autoplan D1).
TARGET_MRR_KEYWORD = 0.85


def _reciprocal_rank(ranked_ids: List[str], expected_ids: List[str]) -> float:
    """1/rank of the first relevant id in ranked_ids, else 0.0."""
    expected = set(expected_ids)
    for i, cid in enumerate(ranked_ids, start=1):
        if cid in expected:
            return 1.0 / i
    return 0.0


def _write_eval_card(
    dest_dir: Path,
    *,
    stem: str,
    source_id: str,
    author: str,
    summary: str,
    tags: List[str],
    body: str,
) -> None:
    """Write a minimal, load_card-valid bookmark card (.md + .raw.txt sidecar)
    into dest_dir. The sidecar bytes equal the body text and raw_checksum is
    computed over them so storage.corpus.load_card() verifies cleanly.

    source_id must be unique per card — bookmark dedup (corpus.iter_cards)
    drops later cards that share a source_id, which would silently shrink the
    distractor set."""
    raw_bytes = body.encode("utf-8")
    checksum = sidecar.compute_checksum(raw_bytes)
    tag_list = ", ".join(tags)
    md = (
        "---\n"
        "source_type: bookmark\n"
        f"source: https://x.com/{author.lstrip('@')}/status/{source_id}\n"
        f"source_id: '{source_id}'\n"
        "source_status: live\n"
        f"author: '{author}'\n"
        "date: 2026-05-01T12:00:00Z\n"
        "captured: 2026-05-01T12:30:00Z\n"
        f"tags: [{tag_list}]\n"
        f"why_saved: 'hard-negative distractor for eval'\n"
        f"retrieval_summary: '{summary}'\n"
        f"retrieval_tags: [{tag_list}]\n"
        f"raw_path: ./{stem}.raw.txt\n"
        f"raw_checksum: {checksum}\n"
        "---\n"
        "## Content\n\n"
        f"{body}\n"
    )
    (dest_dir / f"{stem}.md").write_text(md, encoding="utf-8")
    (dest_dir / f"{stem}.raw.txt").write_bytes(raw_bytes)


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


@pytest.fixture
def eval_corpus_collection(cards_fixture_dir: Path, tmp_path: Path):
    """Build a tmp eval corpus = a copy of the fixture cards + HARD_NEGATIVES,
    and index it as a throwaway qmd collection. Keeps the shared fixture dir
    (used by test_corpus / test_card_model / test_retrieval_engine) untouched.

    Yields (collection_name, corpus_dir).
    """
    if not _INTEGRATION:
        pytest.skip("XSENSAI_RUN_INTEGRATION not set")
    qmd_bin = os.environ.get("XSENSAI_QMD_PATH") or shutil.which("qmd")
    if not qmd_bin:
        pytest.skip("qmd binary not found ($XSENSAI_QMD_PATH / PATH)")

    corpus_dir = tmp_path / "eval_corpus"
    corpus_dir.mkdir()
    # Copy every fixture card (.md + .raw.txt sidecars) so load_card verifies.
    for f in cards_fixture_dir.iterdir():
        if f.is_file():
            shutil.copy2(f, corpus_dir / f.name)
    for i, hn in enumerate(HARD_NEGATIVES):
        _write_eval_card(
            corpus_dir,
            stem=hn["stem"],
            source_id=str(9000 + i),  # unique, non-colliding with fixtures
            author=hn["author"],
            summary=hn["summary"],
            tags=hn["tags"],
            body=hn["body"],
        )

    subprocess.run([qmd_bin, "collection", "remove", "xsensai-eval"],
                   capture_output=True, check=False)
    res = subprocess.run(
        [qmd_bin, "collection", "add", str(corpus_dir),
         "--name", "xsensai-eval", "--mask", "*.md"],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        pytest.fail(f"qmd collection add failed: {res.stderr}")
    yield "xsensai-eval", corpus_dir
    subprocess.run([qmd_bin, "collection", "remove", "xsensai-eval"],
                   capture_output=True, check=False)


@pytest.mark.skipif(not _INTEGRATION, reason="XSENSAI_RUN_INTEGRATION not set")
async def test_eval_mrr_paraphrase_and_hard_negatives(
    eval_corpus_collection, monkeypatch
) -> None:
    """CV-3 honest eval: MRR over keyword + paraphrase queries, against a
    corpus salted with hard-negative distractors.

    Hard gates (must hold on the current BM25-only system):
      - keyword MRR ≥ TARGET_MRR_KEYWORD (rank-sensitive quality on the
        query style the system is actually built for).
      - PRECISION: no hard-negative card may be the rank-1 result for any
        labeled query (a distractor must never beat the true answer outright).

    Tracked diagnostic (NOT gated):
      - paraphrase MRR. BM25 retrieves nothing on zero-overlap queries, so this
        is ~0.00 today by design (vector search off). It is logged every run so
        the lift from the deferred LLM/vector rerank (autoplan D1) is visible.
    """
    collection_name, corpus_dir = eval_corpus_collection
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(corpus_dir))
    monkeypatch.setattr(qmd, "COLLECTION_NAME", collection_name)

    hard_negative_ids = {hn["stem"] for hn in HARD_NEGATIVES}
    labeled: List[Tuple[str, str, List[str]]] = (
        [("keyword", q, ids) for q, ids in GOLDEN_SET]
        + [("paraphrase", q, ids) for q, ids in PARAPHRASE_SET]
    )

    per_query: List[dict] = []
    rr_by_kind: dict = {"keyword": [], "paraphrase": []}
    top1_by_kind: dict = {"keyword": [], "paraphrase": []}
    top3_by_kind: dict = {"keyword": [], "paraphrase": []}
    hard_negative_top1_leaks: List[dict] = []

    for kind, query, expected_ids in labeled:
        results = await engine.search(query, limit=5)
        ids = [h.card.id for h in results.hits]
        rr = _reciprocal_rank(ids, expected_ids)
        exp = set(expected_ids)
        top1 = bool(set(ids[:1]) & exp)
        top3 = bool(set(ids[:3]) & exp)
        rr_by_kind[kind].append(rr)
        top1_by_kind[kind].append(int(top1))
        top3_by_kind[kind].append(int(top3))
        top1_is_hard_neg = bool(ids[:1]) and ids[0] in hard_negative_ids
        if top1_is_hard_neg:
            hard_negative_top1_leaks.append({"query": query, "got": ids[:3]})
        per_query.append({
            "kind": kind, "query": query, "expected": expected_ids,
            "got_top5": ids, "rr": rr, "top1": top1, "top3": top3,
            "top1_hard_negative": top1_is_hard_neg,
        })

    def _mean(xs: list) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    all_rr = rr_by_kind["keyword"] + rr_by_kind["paraphrase"]
    mrr = _mean(all_rr)
    mrr_keyword = _mean(rr_by_kind["keyword"])
    mrr_paraphrase = _mean(rr_by_kind["paraphrase"])
    # §4.2(c): report top-1 / top-3 alongside MRR, over the combined set.
    top1_rate = _mean(top1_by_kind["keyword"] + top1_by_kind["paraphrase"])
    top3_rate = _mean(top3_by_kind["keyword"] + top3_by_kind["paraphrase"])

    EVAL_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "eval": "mrr",
        "n_queries": len(labeled),
        "mrr": mrr,
        "mrr_keyword": mrr_keyword,
        "mrr_paraphrase": mrr_paraphrase,
        "top1_hit_rate": top1_rate,
        "top3_hit_rate": top3_rate,
        "top1_keyword": _mean(top1_by_kind["keyword"]),
        "top3_keyword": _mean(top3_by_kind["keyword"]),
        "top1_paraphrase": _mean(top1_by_kind["paraphrase"]),
        "top3_paraphrase": _mean(top3_by_kind["paraphrase"]),
        "n_hard_negatives": len(HARD_NEGATIVES),
        "hard_negative_top1_leaks": len(hard_negative_top1_leaks),
        "corpus_size": sum(1 for _ in corpus_dir.glob("*.md")),
        "per_query": per_query,
    }
    with EVAL_HISTORY_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")

    print(
        f"\nMRR EVAL: combined mrr={mrr:.3f} top1={top1_rate:.0%} "
        f"top3={top3_rate:.0%} | keyword mrr={mrr_keyword:.3f} "
        f"(gate ≥{TARGET_MRR_KEYWORD:.2f}) | paraphrase mrr={mrr_paraphrase:.3f} "
        f"(diagnostic — BM25 ceiling, autoplan D1) | "
        f"{len(HARD_NEGATIVES)} hard-negatives, "
        f"{len(hard_negative_top1_leaks)} top-1 leaks"
    )
    for row in per_query:
        if row["rr"] < 1.0:
            print(f"  rr={row['rr']:.2f} [{row['kind']}] {row['query']!r} "
                  f"expected={row['expected']} got={row['got_top5'][:3]}")

    assert not hard_negative_top1_leaks, (
        f"Precision failure: hard-negative distractor ranked #1 for "
        f"{len(hard_negative_top1_leaks)} query(ies): {hard_negative_top1_leaks}"
    )
    assert mrr_keyword >= TARGET_MRR_KEYWORD, (
        f"Quality gate failed: keyword MRR {mrr_keyword:.3f} < "
        f"{TARGET_MRR_KEYWORD:.2f}. BM25 should rank keyword-shaped queries "
        "near the top; tune scoring constants or check the index."
    )
