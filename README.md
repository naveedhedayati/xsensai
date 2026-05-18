# x-sensai

> Make your X bookmarks queryable from Claude.

![status: alpha](https://img.shields.io/badge/status-alpha-orange) ![python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue) ![platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey) ![license: MIT](https://img.shields.io/badge/license-MIT-green)

x-sensai turns the bookmarks you save on X into a corpus your AI assistant can search, cite, and reason over. It runs as an [MCP](https://modelcontextprotocol.io) server plus a set of slash commands for [Claude Code](https://docs.claude.com/en/docs/claude-code) and Claude Desktop, so when you ask Claude a question, it can pull from the things you've already curated instead of generic web results.

It's built for X power users who treat their bookmarks like a research library and want to keep using them long after they fall off the timeline.

> ⚠️ **Alpha software.** This is a single-author personal project, public for transparency. APIs and storage formats may change without notice. Issues and pull requests are not currently being accepted — see [Status](#status) below.

---

## Why

X is one of the best content sources on the internet, but it's hostile to retrieval: the search is weak, threads scroll out of reach, and bookmarks pile up faster than you can revisit them. Once a tweet is more than a few weeks old, it's effectively gone.

x-sensai treats your bookmarks as a first-class personal corpus:

- **Sync** pulls new bookmarks (and the threads they belong to) from X via the official API, on a schedule.
- **Cards** are plain markdown files with frontmatter — version-controllable, greppable, portable, no proprietary database.
- **Retrieval** runs locally over a [QMD](https://github.com/tobi/qmd)-backed BM25 index, and surfaces results with `[B]` (bookmark) and `[P]` (pinned) reference markers Claude can cite.
- **Synthesis** stays in Claude's hands — there is no server-side LLM key, no usage-based AI billing inside x-sensai itself.

The result: ask Claude something in your wheelhouse, and instead of hallucinating a summary of generic web wisdom, it grounds its answer in the specific posts you already vetted.

---

## How it works

```
   X bookmarks ──► /xsync ──► markdown cards ──► QMD index
   (your account)              (in your vault)    (local BM25)
                                       │                │
                                       ▼                ▼
                                ┌──────────────────────────┐
                                │   x-sensai MCP server    │
                                └──────────────────────────┘
                                            │
                          ┌─────────────────┴─────────────────┐
                          ▼                                   ▼
                  Claude Code slash commands         Claude Desktop / any
                  (/xfind, /xask, /xpaste, …)        MCP-aware client
```

- **Cards** live as `card.md` (frontmatter + rendered body) plus `card.raw.txt` (byte-exact source) sidecar pairs in a directory you control. Treat the directory like an Obsidian vault — git it, sync it, edit by hand if you want.
- **QMD** indexes the corpus and serves fast full-text search.
- **The MCP server** exposes a small set of read and write tools (`search_bookmarks`, `get_bookmark`, `paste_bookmark`, `annotate_card`, `set_pin`, `delete_bookmark`, …). Any MCP-aware client can use them.
- **Slash commands** (`/xfind`, `/xask`, `/xpaste`, etc.) wrap the MCP tools in conversational flows for Claude Code. They're plain markdown files, so you can read or fork them.
- **Cron sync** is optional. A GitHub Actions workflow can run `/xsync` every couple of days, commit new cards to your vault repo, and push — no machine of yours needs to be on for the sync to happen.

---

## Features

| Slash command | What it does |
|---|---|
| `/xfind` | Fast lookup against your corpus, with `[B]`/`[P]` references and recency-weighted ranking. |
| `/xask` | Thinking session: pulls top-3 bookmarks, optionally forks a 30-day web search, hands Claude a grounded synthesis prompt. |
| `/xpaste` | Drop a tweet or thread into Claude and save it as a card. Conversational; partial state recovers if you abort. |
| `/xnote` | Append a timestamped note block to an existing card. |
| `/xpin` | Pin / unpin / list pinned cards; surfaces cards due for re-review. |
| `/xsync` | Pull new bookmarks from X via the official API. |
| `/xextract` | Backfill summaries and tags for any cards left as `extraction_pending`. |
| `/xdelete` | Soft-delete a card via a confirmation handshake. |
| `/xrestore` | Bring back a soft-deleted card. |
| `/xhelp` | List available commands and tools. |

| MCP tool | What it does |
|---|---|
| `search_bookmarks` | Returns `{hits, meta, rendered_markdown}`. Tombstone-aware. |
| `get_bookmark` | Full card detail by id. |
| `paste_bookmark` / `recover_aborted_paste` | Powers `/xpaste`. |
| `annotate_card` | Powers `/xnote`. |
| `set_pin` / `list_pinned` / `due_cards_for_review` | Powers `/xpin`. |
| `delete_bookmark` / `restore_bookmark` / `list_deleted` | Tombstone lifecycle with confirmation-nonce gating. |
| `xask_capabilities` | Read-only deploy-status helper. |
| `ping` | Smoke test. |

A fuller catalog of error codes and override vocabulary is in [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

---

## Requirements

- **macOS.** x-sensai uses macOS-specific facilities (`F_FULLFSYNC` for crash-safe writes, Keychain for credential storage). Linux/Windows aren't supported.
- **Python 3.11+**.
- **[bun](https://bun.sh)** to install QMD.
- **[QMD](https://github.com/tobi/qmd)** as the search backend (`bun install -g @tobilu/qmd`).
- **[Claude Code](https://docs.claude.com/en/docs/claude-code)** or Claude Desktop with MCP support.
- For sync (optional): **an X developer account** with API access. Initial credit purchase is ~$10; steady-state cost for ~50 bookmarks/month is roughly $1.18/month.
- For `/xask` web context (optional): the [`last30days`](https://github.com/mvanhorn/last30days-skill) Claude skill installed at `~/.claude/skills/last30days/`.

---

## Installation

### 1. Clone and install

```bash
git clone https://github.com/naveedhedayati/xsensai.git
cd xsensai
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
```

### 2. Point x-sensai at your corpus directory

```bash
export XSENSAI_CORPUS_PATH=~/path/to/your/bookmarks-vault
export XSENSAI_QMD_PATH=$(which qmd)   # optional override
```

The corpus directory is just a folder of markdown files. If you use Obsidian, point it at a vault subdirectory. If you don't, any empty folder works.

### 3. Bootstrap QMD and install slash commands

```bash
./scripts/install_commands.sh
```

This:
- Creates the `xsensai-cards` QMD collection.
- Copies `commands/*.md` to `~/.claude/commands/`.
- Wires the `permissions.ask` gate for destructive MCP tools into `~/.claude/settings.json`.
- Reports any v1-format cards that need migration.

### 4. Register the MCP server with Claude

Add to your Claude MCP config (Claude Desktop: `~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "xsensai": {
      "command": "/absolute/path/to/xsensai/.venv/bin/xsensai-mcp",
      "env": {
        "XSENSAI_CORPUS_PATH": "/absolute/path/to/your/bookmarks-vault",
        "XSENSAI_QMD_PATH": "/absolute/path/to/qmd"
      }
    }
  }
}
```

Restart Claude.

### 5. Smoke test

In Claude Code, type `/xfind` and search for any keyword. If the corpus is empty, you'll get a `[CORPUS_EMPTY]` message — expected. Add a card with `/xpaste` and try again.

### 6. Set up sync (optional)

To pull bookmarks from X automatically:

```bash
export XSENSAI_X_CLIENT_ID=<your-x-app-client-id>
python -m xsensai.sync.setup_oauth --check    # verify preconditions
python -m xsensai.sync.setup_oauth            # interactive OAuth flow
```

For unattended sync via GitHub Actions cron, follow [docs/CRON_SETUP.md](docs/CRON_SETUP.md).

---

## Usage

### Find something you've already saved

```
/xfind serverless cold starts
```

Returns the top results with `[B]`/`[P]` refs Claude can cite.

### Think with your corpus

```
/xask what's the strongest argument against using LLMs for evals?
```

`/xask` retrieves the top-3 cards, optionally forks a 30-day web search for fresh context, and hands Claude a grounded synthesis prompt.

### Save something on the fly

```
/xpaste
```

Drop in a tweet or thread URL, or paste raw text. x-sensai prompts conversationally for any missing fields and saves a card.

### Ingest from X

```
/xsync                # since last run (default)
/xsync backlog        # everything
/xsync preview        # fetch list, write nothing
```

The full override vocabulary is in `commands/xsync.md` and surfaced via `/xhelp`.

---

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `XSENSAI_CORPUS_PATH` | `~/Documents/Vault/04_areas/x-bookmarks` | Where cards live. |
| `XSENSAI_QMD_PATH` | (auto-detected) | Path to the `qmd` binary. |
| `XSENSAI_X_CLIENT_ID` | — | Your X dev app client_id (required for sync). |
| `XSENSAI_X_CLIENT_SECRET` | — | Required only for X "Confidential" client apps. |
| `XSENSAI_LAST30DAYS_PATH` | `~/.claude/skills/last30days/scripts/last30days.py` | Web fork script for `/xask`. |
| `XSENSAI_XASK_LOG_MODE` | `hash_only` | `off` / `hash_only` / `full` — privacy default for question logs. |
| `XSENSAI_XSYNC_LOG_MODE` | `hash_only` | Same convention for sync logs. |
| `XSENSAI_CRON_API_CAP` | `200` | Per-attempt X API call cap for cron. |
| `XSENSAI_DESTRUCTIVE_BYPASS` | unset | Skip the confirmation-nonce handshake for scripted maintenance. Audit-logged. |

---

## Quality

x-sensai ships a 15-query golden-set evaluation against a fixture corpus.

- **top-1 hit rate: 93%** (14/15 queries return the expected card as #1)
- **top-3 hit rate: 100%**

Run it yourself:

```bash
XSENSAI_RUN_INTEGRATION=1 .venv/bin/pytest tests/eval/golden_set.py -v -s
```

Trend over time: `xsensai-eval-history`.

---

## Tests

```bash
.venv/bin/pytest                                      # unit tests (always-on)
XSENSAI_RUN_INTEGRATION=1 .venv/bin/pytest            # + integration tests (need QMD + last30days)
```

The suite is ~735 tests including verbatim-fuzz cases, prompt-injection canaries, and concurrency races.

---

## Project layout

```
src/xsensai/         Python package
  errors.py          Error contract: [CODE] / cause / attempted / next_action / retryable
  model/             Card data model
  storage/           Corpus iteration + sidecar I/O
  retrieval/         QMD wrapper + scoring + reference formatting
  mcp_server/        MCP server entry point
  sync/              X API ingestion (XDK)
  xask/              /xask orchestrator
  synthesis/         Output template + injection-defense helpers
  web_fork/          last30days subprocess wrapper
  entrypoints/       Headless cron orchestrator
  locks/             File-locking + atomic-write primitives

commands/            Slash command source files
tests/               pytest suite
scripts/             Setup, install, migration helpers
docs/                CRON_SETUP, CONFLICT_RESOLUTION, PERMISSIONS_ASK
.github/workflows/   CI + scheduled sync
```

---

## Roadmap & changelog

Released versions and what shipped in each: [CHANGELOG.md](./CHANGELOG.md).

Open follow-up work and known gaps: [TODOS.md](./TODOS.md).

---

## Troubleshooting

Errors are surfaced through a structured envelope with a stable code, a cause, what was attempted, a suggested next action, and a retryable flag. The full catalog (with what each code means and how to clear it) is in [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

---

## Status

**Alpha. Single-author personal project.** It's running in daily use by the author, but:

- APIs, storage formats, and command vocabulary may change without warning.
- macOS-only.
- No SLA, no release cadence, no support channel.
- **Issues and pull requests are not currently being accepted.** The repo is public for transparency and for anyone curious enough to fork and adapt it for themselves.

If you fork it and ship something interesting, that's great — you don't need to ask.

---

## License

[MIT](./LICENSE).
