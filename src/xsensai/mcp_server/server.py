"""x-sensai MCP server (Slice 0 stub).

CRITICAL: stdio transport uses STDOUT for JSON-RPC protocol traffic. Any
print() or library that writes to stdout corrupts the stream and Claude
Desktop silently disconnects. ALL logging goes to stderr.

Slice 0 ships a single tool: `ping(echo: str) -> str`. Slice 1 adds
search_bookmarks; Slice 3 adds ask_bookmarks and get_bookmark.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] xsensai-mcp: %(message)s",
)
log = logging.getLogger(__name__)

mcp = FastMCP("xsensai")


@mcp.tool()
def ping(echo: str) -> str:
    """Smoke test tool. Returns 'pong: {echo}' so we can verify the
    Claude Desktop -> MCP server -> Python round-trip works end-to-end
    before any real product code lands.
    """
    log.info("ping called with echo=%r", echo)
    return f"pong: {echo}"


def main() -> None:
    """Run the MCP server over stdio. Blocks until Claude Desktop disconnects."""
    log.info("xsensai-mcp starting (stdio transport)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
