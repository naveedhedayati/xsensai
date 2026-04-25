# x-sensai

Personal X bookmark retrieval skill for Claude. MCP server + 8 conversational slash commands that let Claude draw on a curated taste corpus when thinking with the user.

**Spec / source of truth:** `~/Documents/Vault/02_projects/x-sensai/v2-build-spec.md`

**Current slice:** Slice 0 (spikes + skeleton). See `SLICE_0_PLAN.md`.

## Layout

```
src/xsensai/         Python package (importable as `xsensai`)
  errors.py          Error contract: [CODE]/cause/attempted/next/retryable
  mcp_server/        MCP server (currently: stub `ping` tool)
  model/             Card data model (Slice 1)
  storage/           Sidecar I/O (Slice 1/2)
  locks/             Concurrency (Slice 2)
  retrieval/         QMD wrapper + ranking (Slice 1)
  sync/              XDK + sync (Slice 4)
  commands/          Slash command handlers (Slice 1+)

tests/               pytest suite
scripts/             setup.sh wizard + v1->v2 migration (Slice 6)
spikes/              Verification spike results + mobile fixture
.github/workflows/   CI (pytest on push)
```

## Slice 0 — quick start

```bash
# One-time
brew install uv
uv venv
uv pip install -r requirements.txt
uv pip install -e .

# Run tests
pytest

# Run the MCP server (stdio transport)
xsensai-mcp
```

To wire into Claude Desktop, add an entry to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "xsensai": {
      "command": "/absolute/path/to/.venv/bin/xsensai-mcp"
    }
  }
}
```

Restart Claude Desktop. The `ping` tool should be reachable.

## Build slicing

- **Slice 0** (current): spikes + skeleton + `ping` smoke test + `errors.py`.
- **Slice 1**: card model + sidecar storage + retrieval + `search_bookmarks` + `/xfind` + `/xhelp`.
- **Slice 2**: locks + sidecar write + `/xpaste` + `/xnote` + `/xpin`.
- **Slice 3**: `/xask` + last30days web fork + synthesis.
- **Slice 4**: XDK sync + `/xsync` + checkpoint resume.
- **Slice 5**: GitHub Actions cron.
- **Slice 6**: v1→v2 migration + setup wizard.
