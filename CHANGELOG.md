# Changelog

All notable changes to x-sensai are recorded here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), 4-digit semver `MAJOR.MINOR.PATCH.MICRO`.

## [0.2.0.0] - 2026-04-25

### Added
- **`/xfind` slash command** for Claude Code: prompts for a query, searches your bookmark corpus, returns ranked references with `[B]` (bookmarks) / `[P]` (pastes) markers. Supports inline overrides (`no decay`, `skip pins`).
- **`/xhelp` slash command** listing every command and tool available now and planned.
- **`search_bookmarks` MCP tool** reachable from any Claude conversation: ranked top-N matches with structured payload (`hits`, `meta`, `rendered_markdown`).
- **`get_bookmark` MCP tool** to fetch full card detail by id (returned by `search_bookmarks`).
- **v1 read adapter** (`storage/v1_adapter.py`) so the existing vault works on day one without waiting for Slice 6 migration. Handles the canonical-v1, minimal-v1 (`source`+`author`), and manual-note schemas.
- **Card model** (Pydantic): strict source-type invariants, tz-aware UTC datetimes, sha256 sidecar verification, grapheme-cluster-aware reference truncation.
- **Retrieval engine**: async QMD subprocess wrapper, recency-weighted scoring (90-day half-life, future-date clamped, pinned bypass), pin-dominance bound, adaptive fallback (top-score + margin + dispersion).
- **Quality gate**: 15-query golden-set evaluation (top-1 93%, top-3 100% on fixture corpus). `xsensai-eval-history` console script tracks trend over time.
- **Bootstrap + install scripts**: `bootstrap_qmd.sh` (idempotent QMD collection setup) and `install_commands.sh` (content-aware copy with backup-on-edit).
- **Verbatim fuzz fixtures**: round-trip tests for triple-dash bodies, triple-backticks, and `## Content` literals.

### Changed
- `xsensai.errors.XSensaiError` is no longer a frozen dataclass (Python's exception machinery needs `__traceback__` mutation in async contexts).
- New error code `CORPUS_UNAVAILABLE` distinguishes "corpus path missing" from "no matches found."
- `pyproject.toml`: added `python-frontmatter`, `regex`, `pytest-asyncio` to dependencies. Hash-locked via `requirements.txt`.

### Tests
- 77 unit + integration tests, all passing.
- Coverage spans card model, sidecar, corpus iteration with dup-defense, scoring properties, adaptive fallback, format truncation, MCP subprocess round-trip.
- Real-vault smoke (via `/qa`): 26/31 cards loaded; top-3 hit rate 88% on a hand-picked golden set.

## [0.1.0] - 2026-04-25

### Added
- Slice 0 — project skeleton, error contract module (`XSensaiError`), MCP server with `ping` smoke tool.
- Verification spikes: QMD locking story, XDK availability, OAuth rotation behavior.
- CI scaffolding (`.github/workflows/ci.yml`).
