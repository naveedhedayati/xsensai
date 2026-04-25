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
    print("-" * 72)
    print(f"{'timestamp':<22} {'top1':>6} {'top3':>6} {'queries':>8} {'corpus':>8}")
    print("-" * 72)
    for line in last_n:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = row.get("ts", "?")[:22]
        top1 = row.get("top1_hit_rate", 0.0)
        top3 = row.get("top3_hit_rate", 0.0)
        n = row.get("n_queries", 0)
        corpus = row.get("corpus_size", "?")
        print(f"{ts:<22} {top1:>6.0%} {top3:>6.0%} {n:>8} {corpus:>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
