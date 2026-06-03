"""Tests for the xsensai-eval-history CLI printer.

Covers both record shapes (hit-rate and MRR) plus the robustness guards:
non-string `ts` must not crash the render, and a bool metric must not render
as a percentage.
"""

from __future__ import annotations

import json
from pathlib import Path

from xsensai.cli import eval_history


def _write(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_missing_file_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(eval_history, "EVAL_HISTORY_PATH", tmp_path / "none.jsonl")
    assert eval_history.main() == 0
    assert "No eval history" in capsys.readouterr().out


def test_renders_both_hit_and_mrr_rows(tmp_path, monkeypatch, capsys):
    p = tmp_path / "eval-history.jsonl"
    _write(p, [
        {"ts": "2026-06-03T19:00:00", "eval": "hit", "top1_hit_rate": 0.93,
         "top3_hit_rate": 1.0, "n_queries": 15, "corpus_size": 11},
        {"ts": "2026-06-03T20:00:00", "eval": "mrr", "mrr_keyword": 0.967,
         "mrr_paraphrase": 0.0, "top1_hit_rate": 0.61, "top3_hit_rate": 0.65,
         "n_queries": 23, "corpus_size": 16},
    ])
    monkeypatch.setattr(eval_history, "EVAL_HISTORY_PATH", p)
    assert eval_history.main() == 0
    out = capsys.readouterr().out
    assert "hit" in out and "mrr" in out
    assert "93%" in out      # hit row top1
    assert "0.967" in out    # mrr row keyword MRR
    # the hit row carries no MRR columns -> rendered as "-"
    assert "-" in out


def test_non_string_ts_does_not_crash(tmp_path, monkeypatch, capsys):
    """Regression: a record whose `ts` is an int/None must not raise
    TypeError on the `[:22]` slice (str() coercion)."""
    p = tmp_path / "eval-history.jsonl"
    _write(p, [
        {"ts": 12345, "eval": "hit", "top1_hit_rate": 0.9, "top3_hit_rate": 1.0,
         "n_queries": 1, "corpus_size": 1},
        {"ts": None, "eval": "mrr", "mrr_keyword": 0.5, "mrr_paraphrase": 0.0,
         "n_queries": 1, "corpus_size": 1},
    ])
    monkeypatch.setattr(eval_history, "EVAL_HISTORY_PATH", p)
    assert eval_history.main() == 0


def test_bool_metric_renders_dash_not_percent(tmp_path, monkeypatch, capsys):
    """Regression: bool is an int subclass; `true` must render as "-", not 100%."""
    p = tmp_path / "eval-history.jsonl"
    _write(p, [
        {"ts": "x", "eval": "hit", "top1_hit_rate": True, "top3_hit_rate": 0.5,
         "n_queries": 1, "corpus_size": 1},
    ])
    monkeypatch.setattr(eval_history, "EVAL_HISTORY_PATH", p)
    assert eval_history.main() == 0
    assert "100%" not in capsys.readouterr().out


def test_malformed_json_line_is_skipped(tmp_path, monkeypatch, capsys):
    p = tmp_path / "eval-history.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '{"ts":"ok","eval":"hit","top1_hit_rate":0.9,"top3_hit_rate":1.0}\n'
        "NOT JSON\n"
    )
    monkeypatch.setattr(eval_history, "EVAL_HISTORY_PATH", p)
    assert eval_history.main() == 0  # bad line skipped, no crash


def test_empty_file_returns_zero(tmp_path, monkeypatch, capsys):
    p = tmp_path / "eval-history.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    monkeypatch.setattr(eval_history, "EVAL_HISTORY_PATH", p)
    assert eval_history.main() == 0
    assert "empty" in capsys.readouterr().out.lower()