---
type: plan
slice: 0
status: draft
created: 2026-04-25
parent_spec: /Users/naveedhedayati/Documents/Vault/02_projects/x-sensai/v2-build-spec.md
---

# Slice 0 — Spikes + skeleton

**Goal:** Close the autonomous verification spikes, stand up a Python project skeleton with a functional (stub) MCP server registered against Claude Desktop, and hand the user a tiny fixture for mobile spike #1. End of Slice 0, no real product features exist — but every wiring decision is proven and the next slice can land tests, retrieval, and slash commands without yak-shaving.

**Why this slice exists:** the spec's verification spikes are prerequisites, not "before locking the spec." Pushing them and the wiring decisions ahead of any real code keeps the unknowns small. By the end of Slice 0, the only things we don't know about the build are things that depend on actually writing it.

**Out of scope (deferred to later slices):** card data model, sidecar I/O, locks, retrieval, sync, slash command handlers, migration script, setup wizard, OAuth flow, real MCP tools beyond a `ping` smoke test.

**In scope (added by CEO review D2/D3):**
- `errors.py` — the error contract module (`[CODE] / cause / attempted / next / retryable`) ships in Slice 0 with unit tests for format. Every later slice imports it.
- Private GitHub remote `naveedhedayati/xsensai` created via `gh repo create`; initial commit pushed at end of Slice 0.

**Effort:** ~30-45 min agent work + ~5 min user (Claude Desktop restart; optional mobile fixture trial).

---

## Spikes — autonomous (close before anything else)

### Spike #3 — QMD locking story
- Read QMD's source/docs for write-side concurrency: does `qmd update` write the SQLite index atomically, or partial-write under failure?
- Confirm whether QMD has built-in file locking around index writes, or whether external coordination is required.
- **Load-bearing because:** if QMD writes index files non-atomically, the `index_rebuild` lock from the spec doesn't actually protect against torn reads. Contingency: build off-to-the-side index + atomic swap (re-index into a sibling SQLite, `os.rename` over).
- **Output:** finding + recommendation in `spikes/SPIKE_RESULTS.md`.

### Spike #4 — X OAuth refresh-token rotation
- Read X API OAuth 2.0 PKCE docs to confirm: does X rotate the refresh token on every refresh call, or does the same refresh_token continue to work?
- **Load-bearing because:** if rotated, the GitHub Actions cron must persist the new token back somewhere durable each run; the spec's "manual re-auth on rotation" assumption may be wrong.
- **Escalation path documented in spike output:** if rotation IS happening on every use, Slice 4 needs one of (a) `gh secret set` from inside the workflow via a fine-scoped PAT, (b) external secret store (1Password, Doppler), or (c) shorter-loop manual re-auth pattern. Spike #4 result picks one and writes the recommended Slice 4 design notes inline so we don't re-discover the problem in Slice 4.
- **Output:** finding + recommendation. If rotation is real, propose a token-write-back path before Slice 4.

### Spike #6 — XDK + thread fetch cost
- Confirm `pip install xdk` exists on PyPI (initial check shows package exists, supports OAuth 2.0 PKCE).
- Confirm bookmarks endpoint is exposed (`GET /2/users/:id/bookmarks` or equivalent). If not in XDK, fall back to direct HTTP via `httpx`.
- Document thread-fetch cost: API calls per reply chain, latency per call, projected $/sync at typical thread sizes.
- **Output:** finding + recommendation. If thread-walk is expensive, descope thread stitching from v2 — capture only the bookmarked tweet, lazy-fetch on demand.

### Deferred spikes (not autonomous)
- **Spike #1** (mobile slash discovery): Slice 0 produces a tiny fixture repo and instructions; user runs in parallel.
- **Spike #2** (mobile MCP): blocked on the MCP server existing; revisit after Slice 1.
- **Spike #5** (yt-dlp on GHA): not needed until Slice 4/5; deferred.

---

## Project layout

```
/Users/naveedhedayati/Documents/Claude/Projects/xsensai/
├── .gitignore
├── .python-version              # 3.11
├── pyproject.toml               # package metadata, entry points
├── requirements.in              # source deps
├── requirements.txt             # pip-compile --generate-hashes output
├── README.md                    # short; points to spec
├── CLAUDE.md                    # routing rules: spec is source of truth
├── SLICE_0_PLAN.md              # this file
├── src/
│   └── xsensai/
│       ├── __init__.py
│       ├── _version.py
│       ├── errors.py            # error contract: [CODE]/cause/attempted/next/retryable (Slice 0)
│       ├── model/               # Slice 1
│       ├── storage/             # Slice 1/2
│       ├── locks/               # Slice 2
│       ├── retrieval/           # Slice 1
│       ├── mcp_server/
│       │   ├── __init__.py
│       │   ├── __main__.py      # python -m xsensai.mcp_server
│       │   └── server.py        # stub ping tool
│       ├── sync/                # Slice 4
│       └── commands/            # Slice 1+
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   └── test_smoke.py            # asserts server boots, ping dispatches
├── scripts/
│   ├── setup.sh                 # stub
│   └── migrate_v1_to_v2.py      # stub
├── .github/
│   └── workflows/
│       └── ci.yml               # runs pytest on push
└── spikes/
    ├── SPIKE_RESULTS.md
    └── mobile-fixture/          # tiny repo for user's spike #1
        └── .claude/
            └── commands/
                └── hello.md
```

**Decisions baked in here:**
- **Layout: src/-style package**, not flat. `src/xsensai/...` keeps tests honest (must install package to import) and matches modern Python convention.
- **MCP server is a Python module**, run as `python -m xsensai.mcp_server`. Stdio transport (works with Claude Desktop's MCP config).
- **Empty packages staked out for later slices** so the import graph is stable from day one. Each later slice fills its own subdir, no churn to top-level structure.

---

## Dependencies (requirements.in)

```
# MCP server
mcp >= 1.0

# Data model + serialization
pydantic >= 2.0
pyyaml >= 6.0

# Test
pytest >= 8.0
```

Compiled with `uv pip compile --generate-hashes requirements.in -o requirements.txt`. Any future slice that adds a dep updates `requirements.in` and re-compiles.

**Decisions:**
- **`uv` over `pip-tools`** (Eng review D1): drop-in compatible, same hash-locked output, ~10-100x faster regeneration. Installed via `brew install uv` in Slice 0 setup.
- **Defer XDK, httpx, whisper, yt-dlp** to the slice that actually uses them. Don't bloat Slice 0's lock file.
- **No linter / formatter pinned** in Slice 0. We can add ruff in Slice 1 if it earns its keep.
- **Hash-locking from day one.** Spec calls for it; cheap to set up now, painful to retrofit.

---

## MCP server (Slice 0 stub)

`src/xsensai/mcp_server/server.py`:
- Use official `mcp` Python SDK
- Stdio transport
- Single tool: `ping(echo: str) -> str` returns `"pong: {echo}"`
- Module entrypoint: `python -m xsensai.mcp_server`
- **Console-script entry point in `pyproject.toml`:** `xsensai-mcp = "xsensai.mcp_server.__main__:main"`. Claude Desktop config points at the venv's `bin/xsensai-mcp` directly — no Python module-path knowledge needed in user-facing config.

**Critical MCP stdio gotcha:** the stdio transport uses **stdout for protocol traffic**. Any `print()` or library that writes to stdout corrupts the JSON-RPC stream and Claude Desktop silently disconnects. **All logging goes to stderr only.** Use `logging.basicConfig(stream=sys.stderr, level=logging.INFO)`. Never `print(...)` from anywhere reachable by tool code. This is a famous failure mode worth front-loading.

**`errors.py` API shape:** frozen dataclass `XSensaiError(code: str, cause: str, attempted: str, next_action: str, retryable: bool, details: str | None = None)` with a `format()` method that emits the spec's `[CODE] {cause}\nWhat was attempted: {attempted}\nSafe next action: {next_action}\nRetryable: {yes|no}\n{details if present}` shape. Code is constrained to a `Literal[...]` type listing every code from the spec's error matrix. Construction with an unknown code raises at type-check + runtime — no silent typos.

**Why a stub tool, not just a server with no tools:** if the server registers but no tool is callable, we haven't verified the round-trip. `ping` is the smoke test that proves Claude Desktop → MCP server → Python code → response works end-to-end.

**Logging:** server logs to stderr at `INFO` level on tool dispatch (e.g., `"ping called with echo=hello"`). When Claude Desktop says the MCP is broken, you can `tail -f` the stderr to see what's happening. Pure-Python `logging` module, no extra deps.

**Claude Desktop registration:**
- Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
- Add an `xsensai` server entry pointing to the venv's Python and the module
- User restarts Claude Desktop
- User confirms by typing "ping with echo=hello" or similar in a Claude conversation

This is the only manual step the user does in Slice 0.

---

## Tests (Slice 0 baseline)

- `tests/test_smoke.py`:
  - Imports `xsensai.mcp_server.server` without error
  - Boots the server as a subprocess over stdio (matches how Claude Desktop runs it) and exchanges one `tools/list` + one `tools/call ping` JSON-RPC message
  - Asserts `tools/list` includes `ping` with a valid schema (catches "registered but not introspectable" bugs)
  - Asserts `tools/call` returns the expected `pong: {echo}` payload
  - Subprocess-based smoke is the only way to catch the stdout-pollution gotcha (in-process testing bypasses the transport)
- `tests/test_errors.py`:
  - For each error code shape, asserts the rendered message matches `[CODE] {cause}` and contains the four required lines (Attempted, Safe next action, Retryable, optional details)
  - Asserts `retryable` is a strict bool
  - Asserts unknown codes raise on construction (no silent typos)

- `pytest` runs green from `pytest tests/`.

**Why these tests at this stage:** they prove the package is importable, the MCP scaffold is wired correctly, and we have a CI lane for later slices to add to. No verbatim-corpus / lock / retrieval tests yet — those land with their respective code.

---

## CI (.github/workflows/ci.yml)

- On push: set up Python 3.11, install from hash-locked `requirements.txt`, install package in editable mode, run `pytest`.
- **Runner: `ubuntu-latest`** (Eng review D2). Mac-specific paths (Keychain, Claude Desktop config) aren't exercised in CI; pure-Python tests run identically on Linux at ~3-4x faster job startup.
- This is *not* the sync cron workflow (that's Slice 5).

---

## Git

- `git init` in the project directory.
- `.gitignore`: `.venv/`, `__pycache__`, `*.pyc`, `.pytest_cache/`, `*.egg-info/`, `dist/`, `build/`.
- **Create private GitHub remote** via `gh repo create naveedhedayati/xsensai --private --source=. --remote=origin`.
- One initial commit at end of Slice 0: "slice 0: spikes + skeleton + error contract".
- Push to remote so Slices 1+ have durable storage. Slice 5 just adds `.github/workflows/sync.yml` later — no remote-setup ceremony.

---

## Mobile spike #1 fixture

`spikes/mobile-fixture/` is a self-contained tiny repo for the user to test mobile slash discovery:

- `.claude/commands/hello.md` containing one trivial command that asks Claude to say "hi from mobile."
- A `MOBILE_SPIKE_README.md` with three steps: (1) clone or copy this folder onto your phone via Working Copy / git clone, (2) open in mobile Claude Code, (3) try invoking `/hello` and report whether it autocompletes / runs.

User runs this in parallel with our Slice 1 work. Result feeds Slice 2 or 3 surface decisions.

---

## End-of-slice verification (definition of done)

1. `pytest tests/` green from a fresh `python -m venv .venv && pip install -r requirements.txt && pip install -e .`
2. `python -m xsensai.mcp_server` starts and accepts a stdio MCP handshake without error
3. `claude_desktop_config.json` updated and Claude Desktop, after restart, lists `xsensai` as a connected server
4. From a Claude conversation: a request like "use the xsensai server's ping tool with echo=hello" returns "pong: hello"
5. `spikes/SPIKE_RESULTS.md` has explicit findings for #3, #4, #6 with one-line decisions for the slices that depend on them
6. `spikes/mobile-fixture/` exists with README ready for the user
7. `errors.py` module shipped with passing tests for the contract format
8. Private GitHub remote created and initial commit pushed

---

## Decisions surfaced (NOT silent)

These are the architectural choices baked into Slice 0; flagging in advance, not making silently:

1. **Wrap QMD (path A from earlier discussion), don't layer on `qmd mcp`.** Our MCP server owns recency / pin / fallback / verbatim references / error contract. QMD is a subprocess call we make.
2. **Single MCP server**, registered as `xsensai` in Claude Desktop. Slash commands and the MCP server share the same Python package; slash commands import retrieval directly without going through MCP.
3. **No GitHub remote in Slice 0.** Local git only.
4. **Hash-locked deps from day one** (cheap now, retrofit later is painful).
5. **`src/`-layout package**, not flat repo.
6. **Defer all OAuth and X API code** until Slice 4 — Slice 0 doesn't need to know whether the token rotates yet, only documents the spike answer.

---

## Risks in Slice 0 itself

- **MCP SDK version pin drift.** `mcp >= 1.0` is broad; could pull a newer version with API changes mid-build. Mitigation: pin to whatever pip resolves the first time and let pip-compile capture the hash.
- **Claude Desktop config path varies.** macOS-only path used. setup.sh in Slice 6 will handle Linux/Windows if ever needed; for now, hard-coded macOS path is honest.
- **Spike #4 may be answer-less from public docs.** If X's docs don't say whether refresh tokens rotate, Spike #4 outcome is "test empirically in Slice 4 against a real token." That's fine; document the unknown explicitly.

---

## What Slice 1 inherits from Slice 0

- Working venv, MCP server scaffold to add tools to, error contract module already wired (`from xsensai.errors import ...`), passing test suite, decided spike outcomes, private GitHub repo with first commit pushed. Slice 1 starts by writing the card model and adding `search_bookmarks` next to `ping`. No yak-shaving.

---

## CEO Review — Completion Summary

| Section | Outcome |
|---|---|
| Mode | HOLD SCOPE (infra slice; expansion-vs-hold doesn't meaningfully apply) |
| 0A — Premise challenge | Survived; one real fork surfaced (D1) |
| 0C-bis — Approaches | A chosen: full skeleton + 3 spikes + stub `ping` MCP |
| Cherry-picks accepted | D2: `errors.py` ships in Slice 0. D3: private GitHub remote created in Slice 0 |
| Section 1 (Architecture) | No issues |
| Section 2 (Errors) | Contract module added (D2); no other gaps |
| Section 3 (Security) | N/A — no secrets, no inputs, no untrusted surface in Slice 0 |
| Section 4 (Data flow) | N/A — no data flow yet |
| Section 5 (Code quality) | No findings yet (mostly empty modules) |
| Section 6 (Tests) | `test_errors.py` added; `tools/list` introspection check added to smoke test |
| Section 7 (Perf) | N/A |
| Section 8 (Observability) | stderr `INFO` logging added to stub MCP server |
| Section 9 (Deploy) | No deploy in Slice 0 |
| Section 10 (Long-term) | Reversibility 5/5; no path-dependency lock-in |
| Section 11 (Design) | SKIPPED — no UI scope |
| Outside voice | Skipped — overhead doesn't earn its keep on a 30-60 min infra slice |
| Unresolved decisions | None |

**Verdict:** CLEARED for Eng review.

**Net delta from CEO review:**
- Slice 0 effort estimate revised: ~30-45 min → ~50-75 min agent work + ~5 min user (Claude Desktop restart).
- Two non-trivial additions: the error contract module + GitHub remote bootstrap.
- Three minor additions: error contract tests, MCP `tools/list` introspection check, stderr logging on the stub server.

Net impact on later slices: Slices 1-6 inherit a richer foundation. The error contract is the highest-leverage add — every later slice's user-visible errors are guaranteed to share shape from import-time, eliminating a class of late-discovery inconsistency.

---

## Eng Review — Completion Summary

| Section | Outcome |
|---|---|
| Step 0 — Scope challenge | Scope accepted as-is; no reduction needed (<8 files, no new classes) |
| Section 1 (Architecture) | No issues remaining after applying decisions |
| Section 2 (Code quality) | No issues; `errors.py` shape is explicit > clever |
| Section 3 (Tests) | Coverage diagram: 5/5 paths covered, 0 gaps, 0 regressions |
| Section 4 (Performance) | N/A — no hot paths in Slice 0 |
| Outside voice | Skipped — overhead doesn't earn its keep on a 50-75 min infra slice |
| Worktree parallelization | Sequential — no parallelization opportunity |
| Unresolved decisions | None |
| Critical gaps | None |
| Issues found | 2 real decisions (D1 uv, D2 ubuntu CI), both resolved |

**Decisions accepted:**
- **D1:** `uv` replaces `pip-compile`. Same hash-locked output, ~10-100x faster. `brew install uv` added to setup steps.
- **D2:** CI runs on `ubuntu-latest`, not macOS. Pure-Python tests; Mac-specific paths aren't exercised in CI anyway.

**Documented (no decision, just spec'd):**
- MCP stdio gotcha: stderr-only logging, subprocess-based smoke test to catch any stdout pollution.
- `xsensai-mcp` console-script entry point in `pyproject.toml`; Claude Desktop config points at the venv binary.
- `errors.py` API: frozen dataclass with `Literal` code type; format() emits the spec's 5-line shape.
- Spike #4 escalation: if X rotates refresh tokens on use, the spike output writes the recommended Slice 4 remediation (gh-secret-from-workflow / external secret store / short-loop re-auth) so we don't re-discover the problem mid-Slice-4.

**Verdict:** CLEARED for implementation. Ready to start Slice 0.
