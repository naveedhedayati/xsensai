"""Smoke test for the MCP server.

Boots `xsensai-mcp` as a subprocess (matches how Claude Desktop runs it),
exchanges JSON-RPC messages over stdio, and asserts:

  1. The server starts without crashing.
  2. `tools/list` returns `ping` with a valid schema.
  3. `tools/call ping` round-trips and returns "pong: {echo}".

Subprocess matters: any stray stdout pollution (a `print()` somewhere) breaks
the JSON-RPC stream. An in-process test would miss that. Catching the gotcha
here costs ~50ms per test run and saves hours of "why doesn't Claude Desktop
see my server" debugging.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Any

PING_INPUT_SCHEMA_REQUIRED_KEYS = {"type", "properties"}


def _send(proc: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    payload = (json.dumps(message) + "\n").encode("utf-8")
    assert proc.stdin is not None
    proc.stdin.write(payload)
    proc.stdin.flush()


def _recv(proc: subprocess.Popen[bytes], timeout_s: float = 5.0) -> dict[str, Any]:
    """Read one JSON-RPC line from stdout. Times out if the server hangs."""
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
    raise TimeoutError("MCP server did not respond within timeout")


def _initialize(proc: subprocess.Popen[bytes]) -> None:
    """Run the MCP initialize handshake."""
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "0.0.0"},
            },
        },
    )
    resp = _recv(proc)
    assert resp.get("id") == 1
    assert "result" in resp, f"initialize failed: {resp}"
    # Required follow-up notification
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})


def test_server_subprocess_round_trip() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "xsensai.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _initialize(proc)

        # tools/list — assert ping is registered with a valid input schema
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = _recv(proc)
        assert resp.get("id") == 2, resp
        tools = resp["result"]["tools"]
        ping_tool = next((t for t in tools if t["name"] == "ping"), None)
        assert ping_tool is not None, f"ping tool not registered. tools={tools}"
        schema = ping_tool["inputSchema"]
        assert PING_INPUT_SCHEMA_REQUIRED_KEYS.issubset(schema.keys()), schema
        assert "echo" in schema["properties"], schema

        # tools/call ping — assert round-trip
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "ping", "arguments": {"echo": "hello"}},
            },
        )
        resp = _recv(proc)
        assert resp.get("id") == 3, resp
        result = resp["result"]
        # MCP returns a list of content blocks; ping returns a single text block.
        content = result.get("content", [])
        assert content, f"empty content: {result}"
        text = content[0].get("text") or content[0].get("content", "")
        assert text == "pong: hello", f"unexpected response: {text!r}"
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_server_imports_without_error() -> None:
    """Catches import-time failures (missing deps, syntax errors) without
    needing to spawn a subprocess."""
    from xsensai.mcp_server import server  # noqa: F401
