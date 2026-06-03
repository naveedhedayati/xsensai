"""xsensai-eval-history — print last N quality-gate eval results.

Reads ~/.cache/xsensai/eval-history.jsonl (one JSON object per line, written
by tests/eval/golden_set.py). Shows trend over time so degradation is visible.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL_HISTORY_PATH = Path.home() / ".cache" / "xsensai" / "eval-history.jsonl"


def main() -> int:
    if not EVAL_HISTORY_PATH.exists():
        print(f"No eval history yet. Expected: {EVAL_HISTORY_PATH}")
        print("Run: pytest tests/eval/golden_set.py")
        return 0

    lines = EVAL_HISTORY_PATH.read_text().strip().splitlines()
    if not lines:
        print(f"Eval history file is empty: {EVAL_HISTORY_PATH}")
        return 0

    last_n = lines[-10:]
    print(f"x-sensai eval history (last {len(last_n)} of {len(lines)} runs)")
    print("-" * 84)
    # Two record shapes share this file: the hit-rate gate (top1/top3) and the
    # MRR gate (CV-3: keyword MRR + paraphrase-MRR diagnostic). Show both;
    # "-" marks a column that record doesn't carry.
    print(
        f"{'timestamp':<22} {'eval':>5} {'top1':>6} {'top3':>6} "
        f"{'mrr_kw':>7} {'mrr_pp':>7} {'queries':>8} {'corpus':>7}"
    )
    print("-" * 84)
    for line in last_n:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = str(row.get("ts", "?"))[:22]
        kind = row.get("eval", "hit")
        n = row.get("n_queries", 0)
        corpus = row.get("corpus_size", "?")

        def _num(v) -> bool:
            # bool is an int subclass; a stray `true` must render as "-", not 100%.
            return isinstance(v, (int, float)) and not isinstance(v, bool)

        def _pct(key: str) -> str:
            v = row.get(key)
            return f"{v:.0%}" if _num(v) else "-"

        def _mrr(key: str) -> str:
            v = row.get(key)
            return f"{v:.3f}" if _num(v) else "-"

        print(
            f"{ts:<22} {kind:>5} {_pct('top1_hit_rate'):>6} "
            f"{_pct('top3_hit_rate'):>6} {_mrr('mrr_keyword'):>7} "
            f"{_mrr('mrr_paraphrase'):>7} {n:>8} {str(corpus):>7}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
