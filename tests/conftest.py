"""Shared pytest fixtures for the xsensai test suite."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


@pytest.fixture
def fixtures_dir() -> Path:
    """Absolute path to tests/fixtures/, resolved relative to this file."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def cards_fixture_dir(fixtures_dir: Path) -> Path:
    """Absolute path to tests/fixtures/cards/."""
    return fixtures_dir / "cards"


@pytest.fixture
def fuzz_fixture_dir(fixtures_dir: Path) -> Path:
    """Absolute path to tests/fixtures/verbatim_fuzz/."""
    return fixtures_dir / "verbatim_fuzz"


@pytest.fixture
def qmd_available() -> bool:
    """True iff the qmd binary is on PATH (or at the configured path).

    Used by tests that gate themselves on real QMD availability:
        @pytest.mark.skipif(not qmd_available(), reason="QMD not installed")
    """
    qmd_path = os.environ.get("XSENSAI_QMD_PATH", "/Users/naveedhedayati/.bun/bin/qmd")
    return shutil.which("qmd") is not None or Path(qmd_path).exists()


@pytest.fixture
def run_integration() -> bool:
    """True iff XSENSAI_RUN_INTEGRATION=1; controls QMD-dependent tests in CI."""
    return os.environ.get("XSENSAI_RUN_INTEGRATION") == "1"


@pytest.fixture
def tmp_corpus(tmp_path: Path, monkeypatch) -> Path:
    """Empty temp corpus dir + XSENSAI_CORPUS_PATH set to point there."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(corpus))
    return corpus
