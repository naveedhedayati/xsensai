"""Tests for storage/slug.py — slugify + disambiguate + content_fingerprint."""

from __future__ import annotations

import pytest

from xsensai.errors import XSensaiError
from xsensai.storage import slug


class TestSlugify:
    def test_basic_lowercase_ascii(self):
        assert slug.slugify("Hello World") == "hello-world"

    def test_strips_leading_trailing_dashes(self):
        assert slug.slugify("---foo bar---") == "foo-bar"

    def test_collapses_runs_of_non_alphanum(self):
        assert slug.slugify("foo!!!bar???baz") == "foo-bar-baz"

    def test_truncates_at_max_len(self):
        long_input = "a" * 100
        result = slug.slugify(long_input, max_len=40)
        assert len(result) == 40

    def test_truncate_does_not_end_on_dash(self):
        # Truncating at 5 of "abcd-efgh" would land on the dash
        result = slug.slugify("abcd efgh", max_len=5)
        assert not result.endswith("-")
        assert result == "abcd"

    def test_unicode_nfkd_normalize_strips_accents(self):
        assert slug.slugify("café résumé") == "cafe-resume"

    def test_emoji_only_returns_untitled(self):
        assert slug.slugify("🎉🚀✨") == "untitled"

    def test_whitespace_only_returns_untitled(self):
        assert slug.slugify("   \t\n  ") == "untitled"

    def test_empty_returns_untitled(self):
        assert slug.slugify("") == "untitled"

    def test_path_traversal_attempt_dashed(self):
        # User content "../../etc/passwd" should NOT produce a slug with slashes
        result = slug.slugify("../../etc/passwd")
        assert "/" not in result
        assert ".." not in result
        assert result == "etc-passwd"

    def test_combining_marks_decomposed(self):
        # é as U+0065 + U+0301 (decomposed) vs U+00E9 (precomposed)
        result_decomposed = slug.slugify("é")  # decomposed é
        result_precomposed = slug.slugify("é")
        assert result_decomposed == result_precomposed

    def test_idempotent(self):
        once = slug.slugify("Hello, World! How are you?")
        twice = slug.slugify(once)
        assert once == twice


class TestDisambiguateSlug:
    def test_returns_base_when_no_collision(self, tmp_path):
        result = slug.disambiguate_slug(tmp_path, "paste-2026-04-25-foo")
        assert result == "paste-2026-04-25-foo"

    def test_appends_dash_2_on_first_collision(self, tmp_path):
        (tmp_path / "paste-2026-04-25-foo.md").touch()
        result = slug.disambiguate_slug(tmp_path, "paste-2026-04-25-foo")
        assert result == "paste-2026-04-25-foo-2"

    def test_appends_dash_3_on_second_collision(self, tmp_path):
        (tmp_path / "paste-2026-04-25-foo.md").touch()
        (tmp_path / "paste-2026-04-25-foo-2.md").touch()
        result = slug.disambiguate_slug(tmp_path, "paste-2026-04-25-foo")
        assert result == "paste-2026-04-25-foo-3"

    def test_pathological_loop_raises_internal_error(self, tmp_path, monkeypatch):
        # Force the cap low so we don't actually create 1000 files.
        monkeypatch.setattr(slug, "MAX_DISAMBIGUATION_ATTEMPTS", 3)
        (tmp_path / "paste-2026-04-25-foo.md").touch()
        (tmp_path / "paste-2026-04-25-foo-2.md").touch()
        (tmp_path / "paste-2026-04-25-foo-3.md").touch()
        with pytest.raises(XSensaiError) as exc:
            slug.disambiguate_slug(tmp_path, "paste-2026-04-25-foo")
        assert exc.value.code == "INTERNAL_ERROR"


class TestContentFingerprint:
    def test_same_content_same_fingerprint(self):
        a = slug.content_fingerprint("hello world")
        b = slug.content_fingerprint("hello world")
        assert a == b

    def test_different_content_different_fingerprint(self):
        a = slug.content_fingerprint("hello world")
        b = slug.content_fingerprint("hello worlds")
        assert a != b

    def test_format_is_sha256_prefix(self):
        result = slug.content_fingerprint("anything")
        assert result.startswith("sha256:")
        # 64 hex chars after the prefix
        assert len(result) == len("sha256:") + 64

    def test_empty_string_has_known_hash(self):
        # sha256("") = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
        result = slug.content_fingerprint("")
        assert result == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_unicode_handled(self):
        result = slug.content_fingerprint("café 🎉")
        assert result.startswith("sha256:")
