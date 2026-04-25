---
description: x-sensai command and tool reference
---

You are running `/xhelp` for x-sensai.

Print exactly this content (no editorializing, no header changes):

---

# x-sensai

Personal X bookmark retrieval skill — MCP server + slash commands for Claude.

## Slash commands

### Available now

| Command | What it does |
|---|---|
| `/xfind` | Fast lookup against your bookmark corpus (uses `search_bookmarks` MCP tool) |
| `/xhelp` | This help |

### Planned

| Command | Slice | What it will do |
|---|---|---|
| `/xpaste` | Slice 2 | Save pasted content as a card |
| `/xnote` | Slice 2 | Annotate an existing card |
| `/xpin` | Slice 2 | Pin / unpin / list pinned cards |
| `/xask` | Slice 3 | Full thinking session: corpus + web fusion + synthesis |
| `/xsync` | Slice 4 | Ingest new bookmarks from X |
| `/xtranscribe` | Slice 4 | Process queued video transcriptions |

## MCP tools (callable from any Claude conversation with MCP configured)

### Available now

| Tool | What it does |
|---|---|
| `search_bookmarks(query, limit, no_decay, include_pinned)` | Returns top-N matches with `[B]`/`[P]` references |
| `get_bookmark(id)` | Fetch full card detail by id |
| `ping(echo)` | Smoke test (Slice 0) |

### Planned

| Tool | Slice | What it will do |
|---|---|---|
| `ask_bookmarks(question, ...)` | Slice 3 | Corpus-only synthesis with cited references |

## Inline `/xfind` overrides

Append to your query:
- `no decay` — disable recency weighting
- `skip pins` — exclude pinned cards from results

## Sync status

Sync is configured in **Slice 4** (GitHub Actions cron + XDK ingestion).
Until then, the corpus is whatever you put in `$XSENSAI_CORPUS_PATH`
(defaults to `~/Documents/Vault/04_areas/x-bookmarks/`).

## Eval history

Quality-gate history (top-3 hit rate over time) lives at
`~/.cache/xsensai/eval-history.jsonl`. Run `xsensai-eval-history` to view
the last 10 runs.

## Troubleshooting

See `TROUBLESHOOTING.md` in the project root, keyed by error code.
