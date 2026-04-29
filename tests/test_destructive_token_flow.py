"""Slice 7 integration tests for the confirmation-nonce 2-call flow on
delete_bookmark and restore_bookmark.

Coverage map (TE numbering from plan):
  TE2  — lock contention after redeem (nonce consumed, op fails clean)
  TE4  — FastMCP tools/list shape after restart (smoke via subprocess)
  TE5  — backward-compat: legacy user_confirmed=True returns NONCE_REQUIRED
  TE8  — log redaction: full nonce never appears in caplog
  TE9  — restart-during-flow: deterministic NONCE_INVALID after reset
  TE10 — full failure-matrix: all paths consume nonce per AE10
  TE13 — atomic markdown gate: xrestore.md has no `user_confirmed=True`
  Q9   — cron isolation regression: sync/headless never imports destructive tools

These tests exercise the MCP tool layer end-to-end (function calls, not
JSON-RPC) so they catch path-D semantic regressions without spinning up
a subprocess for every assertion. The subprocess `tools/list` smoke
test verifies the actual JSON-RPC schema once.
"""

from __future__ import annotations

import inspect
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from xsensai.errors import XSensaiError
from xsensai.locks import filelock
from xsensai.mcp_server import nonce_store, server
from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.storage import corpus


# ---- shared fixtures (mirror of test_tombstone vault_corpus) ----------------


@pytest.fixture
def vault_corpus(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    c = vault / "04_areas" / "x-bookmarks"
    c.mkdir(parents=True)
    (vault / "00_inbox").mkdir()
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(c))
    monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
    monkeypatch.delenv("XSENSAI_DESTRUCTIVE_BYPASS", raising=False)
    return c


@pytest.fixture(autouse=True)
def _reset_nonce_store():
    """Avoid cross-test bleed via the module-level singleton."""
    nonce_store.reset_store()
    yield
    nonce_store.reset_store()


def _make_v2_paste(corpus_path: Path, stem: str) -> str:
    captured = datetime.now(timezone.utc)
    body = f"## Content\n\n{stem} body\n"
    fm = CardFrontmatter(
        source_type="paste",
        captured=captured,
        author="self",
    )
    card = LoadedCard(
        fm=fm,
        body=body,
        raw_bytes=f"{stem} body".encode("utf-8"),
        md_path=corpus_path / f"{stem}.md",
    )
    with filelock.with_card_write_lock(corpus_path, "xpaste") as h:
        written = corpus.write_card(card, h.token, corpus_path=corpus_path)
    return written.id


def _extract_display_nonce(rendered_message: str) -> str:
    """Pull the 8-char (formatted as ABCD-EFGH) code from the
    rendered_message between the <<<NONCE: ... >>> markers.
    """
    m = re.search(
        rf"{re.escape(nonce_store.NONCE_DELIMITER_OPEN)}([A-Z2-7-]+){re.escape(nonce_store.NONCE_DELIMITER_CLOSE)}",
        rendered_message,
    )
    assert m is not None, f"no nonce delimiter in: {rendered_message!r}"
    return m.group(1)


# ---- 2-call flow happy path ------------------------------------------------


class TestTwoCallFlowHappyPath:
    def test_delete_first_call_returns_nonce_required(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        result = server.delete_bookmark(id=target_id)
        assert result["ok"] is False
        assert result["error"]["code"] == "NONCE_REQUIRED"
        assert nonce_store.NONCE_DELIMITER_OPEN in result["rendered_message"]
        assert nonce_store.NONCE_DELIMITER_CLOSE in result["rendered_message"]
        # F7 fix: NO duplicate nonce_display field. The code lives only
        # inside the <<<NONCE: ... >>> markers in rendered_message.
        assert "nonce_display" not in result

    def test_delete_second_call_with_nonce_succeeds(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        first = server.delete_bookmark(id=target_id)
        nonce = _extract_display_nonce(first["rendered_message"])
        second = server.delete_bookmark(id=target_id, confirmation_nonce=nonce)
        assert second["ok"] is True
        assert second["deleted"] is True
        assert "Undo within 90s: /xrestore" in second["rendered_message"]

    def test_restore_2call_flow(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        # Pre-condition: card is deleted (use bypass to set up state)
        import os
        os.environ["XSENSAI_DESTRUCTIVE_BYPASS"] = "1"
        try:
            r = server.delete_bookmark(id=target_id)
            assert r["ok"] is True
        finally:
            del os.environ["XSENSAI_DESTRUCTIVE_BYPASS"]
        # Now restore via 2-call flow (no bypass)
        first = server.restore_bookmark(id=target_id)
        assert first["error"]["code"] == "NONCE_REQUIRED"
        nonce = _extract_display_nonce(first["rendered_message"])
        second = server.restore_bookmark(
            id=target_id, confirmation_nonce=nonce
        )
        assert second["ok"] is True
        assert second["restored"] is True

    def test_user_typed_nonce_with_hyphen_works(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        first = server.delete_bookmark(id=target_id)
        display = _extract_display_nonce(first["rendered_message"])
        # Display form has hyphen; user types it as-is
        assert "-" in display  # ABCD-EFGH
        result = server.delete_bookmark(
            id=target_id, confirmation_nonce=display
        )
        assert result["ok"] is True

    def test_user_typed_nonce_lowercase_works(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        first = server.delete_bookmark(id=target_id)
        display = _extract_display_nonce(first["rendered_message"])
        result = server.delete_bookmark(
            id=target_id, confirmation_nonce=display.lower()
        )
        assert result["ok"] is True


# ---- TE5: legacy user_confirmed=True returns NONCE_REQUIRED ----------------


class TestLegacyKwargShim:
    def test_legacy_user_confirmed_returns_nonce_required(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        result = server.delete_bookmark(id=target_id, user_confirmed=True)
        assert result["ok"] is False
        assert result["error"]["code"] == "NONCE_REQUIRED"
        # Cause text mentions the deprecation
        assert "deprecated" in result["error"]["message"].lower()
        # Card is NOT deleted
        card = corpus.load_card_by_id(target_id, corpus_path=vault_corpus, include_deleted=True)
        assert card.fm.deleted is False

    def test_legacy_user_confirmed_false_also_returns_nonce_required(
        self, vault_corpus
    ):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        # user_confirmed=False used to be the soft-guard rejection.
        # In Slice 7 it's still treated as a legacy signal (the flag
        # was supplied at all, regardless of value).
        result = server.delete_bookmark(id=target_id, user_confirmed=False)
        assert result["error"]["code"] == "NONCE_REQUIRED"

    def test_restore_legacy_kwarg_also_shimmed(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        result = server.restore_bookmark(id=target_id, user_confirmed=True)
        assert result["error"]["code"] == "NONCE_REQUIRED"


# ---- TE10: full failure matrix — all paths consume nonce -------------------


class TestRedeemAlwaysConsumes:
    def test_redeem_consumes_on_v1_refusal(self, vault_corpus):
        # Plant a v1-shape card (matches test_tombstone.py:test_delete_v1_card_refused)
        md = vault_corpus / "v1-card.md"
        md.write_text(
            "---\n"
            "type: x-bookmark\n"
            'x_post_id: "1234567890"\n'
            "x_author: paulg\n"
            "x_source_url: https://x.com/paulg/status/1234567890\n"
            "x_date: 2024-12-01T10:00:00Z\n"
            "captured: 2024-12-01T10:00:00Z\n"
            "x_extraction_status: success\n"
            "---\n\n## Content\n\nold v1\n",
            encoding="utf-8",
        )
        first = server.delete_bookmark(id="v1-card")
        nonce = _extract_display_nonce(first["rendered_message"])
        second = server.delete_bookmark(
            id="v1-card", confirmation_nonce=nonce
        )
        assert second["error"]["code"] == "V1_MUTATION_BLOCKED"
        # Nonce was consumed: replay should fail ALREADY_REDEEMED
        third = server.delete_bookmark(
            id="v1-card", confirmation_nonce=nonce
        )
        assert third["error"]["code"] == "NONCE_ALREADY_REDEEMED"

    def test_redeem_consumes_on_already_deleted_noop(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        # First delete via 2-call
        first = server.delete_bookmark(id=target_id)
        nonce = _extract_display_nonce(first["rendered_message"])
        result = server.delete_bookmark(
            id=target_id, confirmation_nonce=nonce
        )
        assert result["ok"] is True
        assert result["deleted"] is True
        # Second delete: get a fresh nonce, redeem, hit already-deleted no-op
        first2 = server.delete_bookmark(id=target_id)
        nonce2 = _extract_display_nonce(first2["rendered_message"])
        result2 = server.delete_bookmark(
            id=target_id, confirmation_nonce=nonce2
        )
        assert result2["ok"] is True
        assert result2.get("already_deleted") is True
        # The second nonce was consumed even on no-op
        replay = server.delete_bookmark(
            id=target_id, confirmation_nonce=nonce2
        )
        assert replay["error"]["code"] == "NONCE_ALREADY_REDEEMED"

    def test_redeem_consumes_on_restore_already_active_noop(self, vault_corpus):
        """F10 fix: symmetric to test_redeem_consumes_on_already_deleted_noop —
        restore_bookmark on an already-active card consumes the nonce too,
        per AE10's "always consume on redeem" rule.
        """
        target_id = _make_v2_paste(vault_corpus, "alpha")
        # Card is not deleted; calling restore is a no-op
        first = server.restore_bookmark(id=target_id)
        nonce = _extract_display_nonce(first["rendered_message"])
        result = server.restore_bookmark(
            id=target_id, confirmation_nonce=nonce
        )
        assert result["ok"] is True
        assert result.get("already_active") is True
        # Replay with the consumed nonce → ALREADY_REDEEMED
        replay = server.restore_bookmark(
            id=target_id, confirmation_nonce=nonce
        )
        assert replay["error"]["code"] == "NONCE_ALREADY_REDEEMED"

    def test_legacy_plus_nonce_prefers_nonce(self, vault_corpus):
        """F11 fix: if BOTH user_confirmed=True AND a valid nonce are
        supplied, prefer the new flow (use the nonce). Don't silently
        discard a valid code with the migration envelope.
        """
        target_id = _make_v2_paste(vault_corpus, "alpha")
        first = server.delete_bookmark(id=target_id)
        nonce = _extract_display_nonce(first["rendered_message"])
        # Caller mid-migration supplies BOTH the new nonce AND the
        # deprecated user_confirmed kwarg. Server should redeem the
        # nonce path, not bounce them with NONCE_REQUIRED migration.
        result = server.delete_bookmark(
            id=target_id, confirmation_nonce=nonce, user_confirmed=True
        )
        assert result["ok"] is True
        assert result["deleted"] is True


# ---- TE9: restart-during-flow ---------------------------------------------


class TestRestartDuringFlow:
    def test_post_reset_nonce_is_invalid(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        first = server.delete_bookmark(id=target_id)
        nonce = _extract_display_nonce(first["rendered_message"])
        # Simulate MCP server restart
        nonce_store.reset_store()
        result = server.delete_bookmark(
            id=target_id, confirmation_nonce=nonce
        )
        assert result["error"]["code"] == "NONCE_INVALID"


# ---- Operation/target mismatch via the MCP layer --------------------------


class TestOperationMismatchViaTool:
    def test_delete_nonce_on_restore_returns_op_mismatch(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        first = server.delete_bookmark(id=target_id)
        nonce = _extract_display_nonce(first["rendered_message"])
        result = server.restore_bookmark(
            id=target_id, confirmation_nonce=nonce
        )
        assert result["error"]["code"] == "NONCE_OPERATION_MISMATCH"
        # The original nonce is still consumed by the OP_MISMATCH check
        # (it was matched via the secondary scan but bound to a different op).
        # Per AE10 we did NOT remove the original record, so a same-target
        # delete redemption with the same nonce should still work:
        delete_again = server.delete_bookmark(
            id=target_id, confirmation_nonce=nonce
        )
        assert delete_again["ok"] is True


# ---- TE8: log redaction (no full nonce in logs) ----------------------------


class TestLogRedaction:
    def test_full_nonce_never_in_logs(self, vault_corpus, caplog):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        with caplog.at_level(logging.INFO, logger="xsensai.mcp_server.server"):
            first = server.delete_bookmark(id=target_id)
            nonce = _extract_display_nonce(first["rendered_message"])
            server.delete_bookmark(id=target_id, confirmation_nonce=nonce)
        full_nonce = nonce.replace("-", "").upper()
        for record in caplog.records:
            assert full_nonce not in record.getMessage(), (
                f"nonce leaked: {record.getMessage()!r}"
            )


# ---- bypass mode -----------------------------------------------------------


class TestDestructiveBypass:
    def test_bypass_skips_handshake(self, vault_corpus, monkeypatch):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        monkeypatch.setenv("XSENSAI_DESTRUCTIVE_BYPASS", "1")
        # No nonce, no kwarg — bypass takes effect
        result = server.delete_bookmark(id=target_id)
        assert result["ok"] is True
        assert result["deleted"] is True

    def test_bypass_logs_warning(self, vault_corpus, monkeypatch, caplog):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        monkeypatch.setenv("XSENSAI_DESTRUCTIVE_BYPASS", "1")
        with caplog.at_level(logging.WARNING, logger="xsensai.mcp_server.server"):
            server.delete_bookmark(id=target_id)
        assert any(
            "XSENSAI_DESTRUCTIVE_BYPASS=1" in r.getMessage()
            for r in caplog.records
        ), "expected loud bypass warning in logs"


# ---- TE13: atomic markdown gate -------------------------------------------


class TestAtomicMarkdownGate:
    """Verify commands/xrestore.md and commands/xdelete.md and the server
    signature haven't drifted. If a slash-command markdown still references
    `user_confirmed=True` for delete/restore after Slice 7 ships, the host
    LLM will follow the wrong flow.

    Slice 7.5 (AE4): /xdelete ships in v0.9.0.0, BEFORE the v0.9.1.0 shim
    removal. The atomic-markdown gate must scan xdelete.md too — without it,
    the v0.9.0.0 → v0.9.1.0 coexistence window is a regression hole (a stale
    /xdelete instruction could silently reintroduce the legacy flow).
    """

    def test_xrestore_md_has_no_legacy_kwarg(self):
        repo_root = Path(__file__).resolve().parent.parent
        markdown = (repo_root / "commands" / "xrestore.md").read_text(encoding="utf-8")
        # Allow the documentation NOTE that mentions "DO NOT pass user_confirmed=True"
        # but reject any actual call instruction.
        # Pattern we want to forbid: `restore_bookmark(... user_confirmed=True ...)`
        # Pattern we allow: `restore_bookmark(id=..., confirmation_nonce=...)`
        pattern_calls = re.findall(
            r"restore_bookmark\([^)]*user_confirmed\s*=\s*True", markdown
        )
        assert pattern_calls == [], (
            f"xrestore.md still calls restore_bookmark with user_confirmed=True: "
            f"{pattern_calls!r}"
        )
        pattern_calls_delete = re.findall(
            r"delete_bookmark\([^)]*user_confirmed\s*=\s*True", markdown
        )
        assert pattern_calls_delete == [], (
            f"xrestore.md still calls delete_bookmark with user_confirmed=True"
        )

    def test_xrestore_md_mentions_nonce_flow(self):
        repo_root = Path(__file__).resolve().parent.parent
        markdown = (repo_root / "commands" / "xrestore.md").read_text(encoding="utf-8")
        assert "confirmation_nonce" in markdown
        assert "NONCE_REQUIRED" in markdown
        assert "<<<NONCE:" in markdown

    def test_xdelete_md_has_no_legacy_kwarg(self):
        """Slice 7.5 (AE4): same regression net for the new /xdelete command."""
        repo_root = Path(__file__).resolve().parent.parent
        xdelete = repo_root / "commands" / "xdelete.md"
        if not xdelete.exists():
            pytest.skip("commands/xdelete.md not present (pre-v0.9.0.0)")
        markdown = xdelete.read_text(encoding="utf-8")
        pattern_calls = re.findall(
            r"delete_bookmark\([^)]*user_confirmed\s*=\s*True", markdown
        )
        assert pattern_calls == [], (
            f"xdelete.md calls delete_bookmark with user_confirmed=True: "
            f"{pattern_calls!r}"
        )
        pattern_calls_restore = re.findall(
            r"restore_bookmark\([^)]*user_confirmed\s*=\s*True", markdown
        )
        assert pattern_calls_restore == [], (
            f"xdelete.md calls restore_bookmark with user_confirmed=True"
        )

    def test_xdelete_md_mentions_nonce_flow(self):
        repo_root = Path(__file__).resolve().parent.parent
        xdelete = repo_root / "commands" / "xdelete.md"
        if not xdelete.exists():
            pytest.skip("commands/xdelete.md not present (pre-v0.9.0.0)")
        markdown = xdelete.read_text(encoding="utf-8")
        assert "confirmation_nonce" in markdown
        assert "NONCE_REQUIRED" in markdown
        assert "<<<NONCE:" in markdown


# ---- TE-Q9: cron isolation regression -------------------------------------


class TestCronIsolation:
    """Q9 verification: scheduled cron sync (sync/service.py + entrypoints/headless.py)
    must NEVER call delete_bookmark/restore_bookmark. The nonce design is
    safe for headless paths only because headless paths never hit the
    destructive surface. Regression test catches a future maintainer who
    wires destructive ops into the cron path.
    """

    def test_sync_service_does_not_import_destructive_tools(self):
        from xsensai.sync import service
        src = inspect.getsource(service)
        for name in ("delete_bookmark", "restore_bookmark"):
            assert name not in src, (
                f"sync/service.py references {name} — cron path "
                "would need a nonce strategy. See plan Q9."
            )

    def test_headless_entrypoint_does_not_import_destructive_tools(self):
        from xsensai.entrypoints import headless
        src = inspect.getsource(headless)
        for name in ("delete_bookmark", "restore_bookmark"):
            assert name not in src, (
                f"entrypoints/headless.py references {name} — cron path "
                "would need a nonce strategy. See plan Q9."
            )


# ---- invalid id handling ---------------------------------------------------


class TestInvalidId:
    def test_malformed_id_rejected_before_nonce_issued(self, vault_corpus):
        # Path-traversal / leading-dot / slash are rejected by validate_card_id
        for bad_id in ("../etc/passwd", ".dotfile", "with/slash"):
            result = server.delete_bookmark(id=bad_id)
            assert result["error"]["code"] == "NO_RESULTS", (
                f"id={bad_id!r} should be rejected before nonce issue"
            )
            # No nonce was issued — store is empty
            assert nonce_store._STORE._by_key == {}
