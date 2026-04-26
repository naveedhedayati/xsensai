"""Tests for the _assert_inside_corpus + validate_card_id security guards.

Slice 2 closes the path-traversal class that Slice 1's load_card_by_id
allowed. These tests verify that user-supplied card ids cannot escape the
corpus root.
"""

from __future__ import annotations

import pytest

from xsensai.errors import XSensaiError
from xsensai.storage import corpus


class TestValidateCardId:
    def test_accepts_basic_alphanumeric(self):
        # No exception
        corpus.validate_card_id("paste-2026-04-25-foo")
        corpus.validate_card_id("2026-04-01-paulg-1234567890")

    def test_accepts_dots_underscores_dashes(self):
        corpus.validate_card_id("foo.bar_baz-quux")

    def test_rejects_empty(self):
        with pytest.raises(XSensaiError) as exc:
            corpus.validate_card_id("")
        assert exc.value.code == "NO_RESULTS"

    def test_rejects_path_separator_forward(self):
        with pytest.raises(XSensaiError):
            corpus.validate_card_id("foo/bar")

    def test_rejects_path_separator_backslash(self):
        with pytest.raises(XSensaiError):
            corpus.validate_card_id("foo\\bar")

    def test_rejects_traversal(self):
        with pytest.raises(XSensaiError):
            corpus.validate_card_id("../etc/passwd")

    def test_rejects_leading_dot(self):
        with pytest.raises(XSensaiError):
            corpus.validate_card_id(".hidden")

    def test_rejects_null_byte(self):
        with pytest.raises(XSensaiError):
            corpus.validate_card_id("foo\x00bar")

    def test_rejects_whitespace(self):
        with pytest.raises(XSensaiError):
            corpus.validate_card_id("foo bar")


class TestAssertInsideCorpus:
    def test_accepts_path_inside_corpus(self, tmp_path):
        corpus_root = tmp_path / "corpus"
        corpus_root.mkdir()
        target = corpus_root / "card.md"
        target.touch()
        result = corpus._assert_inside_corpus(target, corpus_root)
        assert result == target.resolve()

    def test_rejects_path_outside_corpus(self, tmp_path):
        corpus_root = tmp_path / "corpus"
        corpus_root.mkdir()
        outside = tmp_path / "evil.md"
        outside.touch()
        with pytest.raises(XSensaiError) as exc:
            corpus._assert_inside_corpus(outside, corpus_root)
        assert exc.value.code == "NO_RESULTS"

    def test_rejects_traversal_via_dotdot(self, tmp_path):
        corpus_root = tmp_path / "corpus"
        corpus_root.mkdir()
        traversal = corpus_root / ".." / ".." / "etc" / "passwd.md"
        with pytest.raises(XSensaiError):
            corpus._assert_inside_corpus(traversal, corpus_root)


class TestLoadCardByIdRejectsBadIds:
    def test_path_traversal_id_rejected(self, tmp_corpus):
        with pytest.raises(XSensaiError) as exc:
            corpus.load_card_by_id("../../etc/passwd", corpus_path=tmp_corpus)
        assert exc.value.code == "NO_RESULTS"

    def test_empty_id_rejected(self, tmp_corpus):
        with pytest.raises(XSensaiError):
            corpus.load_card_by_id("", corpus_path=tmp_corpus)

    def test_id_with_slash_rejected(self, tmp_corpus):
        with pytest.raises(XSensaiError):
            corpus.load_card_by_id("foo/bar", corpus_path=tmp_corpus)

    def test_valid_id_missing_card_returns_no_results_not_traceback(self, tmp_corpus):
        with pytest.raises(XSensaiError) as exc:
            corpus.load_card_by_id("nonexistent-but-valid-id", corpus_path=tmp_corpus)
        assert exc.value.code == "NO_RESULTS"
