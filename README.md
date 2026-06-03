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
- **Retrieval** runs locally over a [QMD](https://github.com/tobi/qmd)-backed BM25 index, and surfaces results with `[B]` (bookmark) and `[P]` (pasted) reference markers Claude can cite.
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
- **[QMD](https://github.com/tobi/qmd)** as the search backend (`bun install -g qmd`).
- **An MCP host** — [Claude Code](https://docs.claude.com/en/docs/claude-code), Claude Desktop, or **Codex**. Claude Code adds the slash commands (`/xfind`, `/xask`, …); other hosts drive the MCP tools directly (see [AGENTS.md](./AGENTS.md)).
- For sync (optional): **an X developer account** with API access. Initial credit purchase is ~$10; steady-state cost for ~50 bookmarks/month is roughly $1.18/month.
- For `/xask` web context (optional): the [`last30days`](https://github.com/mvanhorn/last30days-skill) Claude skill installed at `~/.claude/skills/last30days/`.

---

## Installation

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/xsensai.git   # your fork
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

### 4. Register the MCP server (Claude Code or Codex)

x-sensai is an MCP server, so it works in any MCP host. All three options below
point at the same `xsensai-mcp` console script. **Codex and Claude Code are both
first-class** — see [AGENTS.md](./AGENTS.md) for the agent-driven guide (the
tool-only path, including the `/xask` `xask_prepare` → synthesize → `xask_validate`
loop a Codex user follows).

- **Claude Code** — `claude mcp add xsensai -- /absolute/path/to/xsensai/.venv/bin/xsensai-mcp`, or just open the repo: a project-scoped `.mcp.json` ships in the root and is auto-discovered.
- **Codex** — add to `~/.codex/config.toml`:
  ```toml
  [mcp_servers.xsensai]
  command = "/absolute/path/to/xsensai/.venv/bin/xsensai-mcp"
  # env = { XSENSAI_CORPUS_PATH = "/absolute/path/to/your/bookmarks-vault" }
  ```
- **Claude Desktop** — add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
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

Restart the host. Then confirm the tools loaded (list tools / `tools/list`) — `xask_prepare` should be present.

### 5. Smoke test

In Claude Code, type `/xfind` and search for any keyword. A brand-new corpus is empty, so you'll get *no results* — that's expected, not an error. Add a card with `/xpaste` (or run sync, below) and try again.

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
| `XSENSAI_CORPUS_PATH` | `~/.local/share/xsensai/corpus` | Where cards live (point this at your Obsidian vault's bookmarks folder). |
| `XSENSAI_QMD_PATH` | (auto-detected) | Path to the `qmd` binary. |
| `XSENSAI_X_CLIENT_ID` | — | Your X dev app client_id (required for sync). |
| `XSENSAI_X_CLIENT_SECRET` | — | Required only for X "Confidential" client apps. |
| `XSENSAI_LAST30DAYS_PATH` | `~/.claude/skills/last30days/scripts/last30days.py` | Web fork script for `/xask`. |
| `XSENSAI_XASK_LOG_MODE` | `hash_only` | `off` / `hash_only` / `full` — privacy default for question logs. |
| `XSENSAI_XSYNC_LOG_MODE` | `hash_only` | Same convention for sync logs. |
| `XSENSAI_CRON_API_CAP` | `200` | Per-attempt X API call cap for cron. |
| `XSENSAI_SECRETS_PAT` | — | Set as a GitHub Actions secret (not a local env var). Fine-grained PAT with `Secrets:write` on this repo only; lets the cron persist the rotated single-use X refresh token back to the `XSENSAI_X_REFRESH_TOKEN` secret. Required for unattended cron sync — see [docs/CRON_SETUP.md](docs/CRON_SETUP.md#token-rotation). |
| `XSENSAI_ALLOW_NO_PERSIST` | unset | Opt out of the fatal-missing-PAT guard so a cron run without `XSENSAI_SECRETS_PAT` doesn't exit 2. |
| `XSENSAI_DESTRUCTIVE_BYPASS` | unset | Skip the confirmation-nonce handshake for scripted maintenance. Audit-logged. |

---

## Quality

x-sensai measures retrieval quality honestly and gates the answer it hands you.

**Retrieval eval** — a golden set against a fixture corpus:

- **15 keyword queries:** top-1 hit rate **93%** (14/15 return the expected card as #1), top-3 hit rate **100%**, keyword MRR **0.97**.
- **8 paraphrase queries** (low literal overlap with the target card): tracked as a diagnostic, not a gate. MRR is ~**0.00** today by design — retrieval is BM25-only (vector search is off), so a zero-overlap query retrieves nothing. This split exposes the semantic ceiling and is logged every run so the lift from a future vector/LLM rerank is visible.
- **5 hard-negative distractor cards** salted into the eval corpus. Precision is hard-gated: a distractor must never outrank the true answer at #1.

**Groundedness gate** — `/xask` answers don't just have to be well-formatted, they have to be backed by your cards. Pass `meta["rerank_winners"]` from `xask_prepare` to `xask_validate` and the answer must either explicitly **abstain** ("your corpus doesn't cover this") or **cite at least 2 distinct cards** AND back every `## Synthesis` line with an inline `[B]/[P]` reference (or a `(no corpus support — general knowledge)` hedge). The check is deterministic and offline — no LLM judge.

Run the eval yourself:

```bash
XSENSAI_RUN_INTEGRATION=1 .venv/bin/pytest tests/eval/golden_set.py -v -s
```

Trend over time: `xsensai-eval-history` (shows keyword and paraphrase MRR alongside hit rates).

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

AGENTS.md            Agent guide (Codex + Claude Code); CLAUDE.md is the Claude-Code twin
commands/            Slash command source files
tests/               pytest suite
scripts/             Setup, install, migration helpers
docs/                ARCHITECTURE, CRON_SETUP, CONFLICT_RESOLUTION, PERMISSIONS_ASK
.github/workflows/   CI + scheduled sync
```

Design detail (card model, retrieval pipeline, why there's no server-side LLM key):
[docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md).

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
