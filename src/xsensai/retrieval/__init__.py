"""Retrieval engine for x-sensai (Slice 1+).

engine: async search orchestrator
qmd: async subprocess wrapper for QMD CLI
scoring: recency curve, pin bypass, adaptive fallback (pure functions)
format: [B]/[P] reference block formatter (grapheme-cluster-aware truncation)
"""

from xsensai.retrieval import engine, format, qmd, scoring

__all__ = ["engine", "format", "qmd", "scoring"]
