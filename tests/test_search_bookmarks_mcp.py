"""Subprocess-based test for search_bookmarks MCP tool.

Boots the MCP server as a subprocess (matches Claude Desktop runtime),
calls tools/list to verify search_bookmarks is registered, then mocks
the corpus path to assert error-shape handling.

Round-trip with real QMD is gated on XSENSAI_RUN_INTEGRATION.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest


def _send(proc: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    payload = (json.dumps(message) + "\n").encode("utf-8")
    assert proc.stdin is not None
    proc.stdin.write(payload)
    proc.stdin.flush()


def _recv(proc: subprocess.Popen[bytes], timeout_s: float = 10.0) -> dict[str, Any]:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.01)
            continue
        text = line.decode("utf-8").strip()
        if not text:
            continue
        return json.loads(text)
    raise TimeoutError("MCP server did not respond")


def _initialize(proc: subprocess.Popen[bytes]) -> None:
    _send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke-test", "version": "0.0.0"},
        },
    })
    resp = _recv(proc)
    assert resp.get("id") == 1
    assert "result" in resp
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})


def _spawn(env_extra: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(
        [sys.executable, "-m", "xsensai.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )


def test_search_bookmarks_registered() -> None:
    """tools/list should include search_bookmarks alongside ping."""
    proc = _spawn()
    try:
        _initialize(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = _recv(proc)
        names = {t["name"] for t in resp["result"]["tools"]}
        assert "ping" in names
        assert "search_bookmarks" in names
        assert "get_bookmark" in names
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_all_slice_2_tools_registered() -> None:
    """tools/list MUST include all Slice 2 write tools — per /review F7
    (API contract specialist) + UC9/UC10/UC11 wire-ups."""
    expected = {
        # Slice 0 + 1
        "ping", "search_bookmarks", "get_bookmark",
        # Slice 2 core
        "paste_bookmark", "annotate_card",
        "set_pin", "list_pinned", "due_cards_for_review",
        # Slice 2 wire-ups (UC9/UC10/UC11) + F22 split
        "recover_aborted_paste",  # deprecated, back-compat
        "list_recoverable_pastes", "get_aborted_paste",  # F22 split
        "write_paste_snapshot", "clear_paste_snapshot",  # UC11 + UC9
        "get_review_cursor", "set_review_cursor",  # UC10
    }
    proc = _spawn()
    try:
        _initialize(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 99, "method": "tools/list"})
        resp = _recv(proc)
        names = {t["name"] for t in resp["result"]["tools"]}
        missing = expected - names
        assert not missing, f"Slice 2 tools missing from tools/list: {missing}"
        # Each tool MUST have a non-empty inputSchema (FastMCP generates from sig)
        for tool in resp["result"]["tools"]:
            assert tool.get("inputSchema"), f"tool {tool['name']!r} has no inputSchema"
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_search_bookmarks_corpus_unavailable() -> None:
    """If the corpus path doesn't exist, search returns CORPUS_UNAVAILABLE."""
    proc = _spawn(env_extra={"XSENSAI_CORPUS_PATH": "/nonexistent/path/foo"})
    try:
        _initialize(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "search_bookmarks",
                "arguments": {"query": "anything", "limit": 5},
            },
        })
        resp = _recv(proc)
        assert resp.get("id") == 3
        result = resp["result"]
        # FastMCP wraps structured returns; pull the JSON content
        text_block = result["content"][0]
        text = text_block.get("text") or json.dumps(text_block)
        # Could be JSON-formatted or plain text — just check the code surfaces
        assert "CORPUS_UNAVAILABLE" in text
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_get_bookmark_missing_id() -> None:
    """get_bookmark on a nonexistent id returns NO_RESULTS."""
    cards_dir = Path(__file__).parent / "fixtures" / "cards"
    proc = _spawn(env_extra={"XSENSAI_CORPUS_PATH": str(cards_dir)})
    try:
        _initialize(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "get_bookmark",
                "arguments": {"id": "definitely-does-not-exist"},
            },
        })
        resp = _recv(proc)
        text_block = resp["result"]["content"][0]
        text = text_block.get("text") or json.dumps(text_block)
        assert "NO_RESULTS" in text
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_get_bookmark_round_trip() -> None:
    """get_bookmark returns full card detail for an existing id."""
    cards_dir = Path(__file__).parent / "fixtures" / "cards"
    proc = _spawn(env_extra={"XSENSAI_CORPUS_PATH": str(cards_dir)})
    try:
        _initialize(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {
                "name": "get_bookmark",
                "arguments": {"id": "2026-04-20-paulg-1234567890"},
            },
        })
        resp = _recv(proc)
        text_block = resp["result"]["content"][0]
        text = text_block.get("text") or json.dumps(text_block)
        assert "paulg" in text
        assert "side projects" in text
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_get_bookmark_includes_is_v1_field() -> None:
    """Slice 7.5 (AE5 fix): get_bookmark exposes top-level `is_v1: bool` so
    /xdelete's id-resolve path can short-circuit on v1 cards before issuing
    a nonce. The response shape MUST include `is_v1` for v2 cards (False)
    and would expose `is_v1: True` for v1 cards if loaded.
    """
    cards_dir = Path(__file__).parent / "fixtures" / "cards"
    proc = _spawn(env_extra={"XSENSAI_CORPUS_PATH": str(cards_dir)})
    try:
        _initialize(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {
                "name": "get_bookmark",
                "arguments": {"id": "2026-04-20-paulg-1234567890"},
            },
        })
        resp = _recv(proc)
        text_block = resp["result"]["content"][0]
        text = text_block.get("text") or json.dumps(text_block)
        # Existing fixtures are v2 (have raw_path/raw_checksum)
        assert "is_v1" in text, f"is_v1 field missing from response: {text[:200]}"
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
