# Troubleshooting

Keyed by error code. Every error in x-sensai routes through `XSensaiError.format()` so you see exactly which one fired.

---

## `[CORPUS_UNAVAILABLE]`

**Cause:** the corpus directory is missing, empty, or not a directory.

**Fix:**
1. Check `$XSENSAI_CORPUS_PATH` (defaults to `~/Documents/Vault/04_areas/x-bookmarks/`).
2. Make sure the directory exists and contains `.md` files.
3. Run `scripts/bootstrap_qmd.sh` to (re-)create the QMD index pointed at it.

If you have v1 cards in there, the v1 adapter loads them automatically (no action needed). If you have no cards yet, wait for Slice 6 migration or `/xpaste` (Slice 2).

---

## `[NO_RESULTS]`

**Cause:** the corpus has cards but your query matched nothing.

**Fix:**
- Try a broader query (drop adjectives, use simpler keywords).
- Remove `no decay` or `skip pins` if you used them — those filter results.
- Check `xsensai-eval-history` to see if quality has regressed.

---

## `[INTERNAL_ERROR]`

**Cause:** something broke in the QMD subprocess wrapper or JSON parser.

**Fix:**
1. Check QMD is installed: `which qmd` or `$XSENSAI_QMD_PATH`. Install with `bun install -g qmd`.
2. Check QMD index health: `qmd status`.
3. If `details` mention "schema drift" or "non-JSON output", QMD updated its CLI shape. Re-spike: `qmd search "test" --json -c xsensai-cards | head` and update `tests/fixtures/qmd_query_output.json` + the parser in `src/xsensai/retrieval/qmd.py`.
4. If it's a timeout (QMD didn't respond in 10s), re-run /xfind. If it persists, the index may be huge or stuck — `qmd status` to investigate.

---

## `[YAML_PARSE_FAILED]`

**Cause:** a card has malformed YAML frontmatter, or the v2 schema rejected it.

**Fix:**
- The card path is in stderr (look at the MCP server's log output).
- Open the card and check the `---` frontmatter at the top.
- Common gotchas:
  - YAML 1.1 traps: use `true`/`false` not `yes`/`no`; quote strings that look like numbers.
  - Datetimes must be timezone-aware ISO-8601 (e.g., `2026-04-25T10:00:00Z`).
  - Bookmark cards require `source`, `source_id`, `author`. Paste cards must NOT have `source_id`.
- v1-shape cards (no `raw_path`/`raw_checksum`) are loaded via the v1 adapter; if a v1 card still fails, its frontmatter has another problem.

---

## `[DISK_WRITE_FAILED]` (sidecar)

**Cause:** sidecar `.raw.txt` is missing, unreadable, or its bytes don't match the recorded checksum.

**Fix:**
- Missing sidecar: restore from git or remove `raw_path`/`raw_checksum` from the card (downgrades to v1-adapter path).
- Checksum mismatch: someone (or some process) edited the `.raw.txt` after the card was created. Restore from git, OR re-compute the checksum manually:
  ```bash
  shasum -a 256 path/to/card.raw.txt
  # update raw_checksum in the .md to "sha256:<the hash>"
  ```

---

## Slash commands (`/xfind`, `/xhelp`) don't appear in Claude Code

**Cause:** install script didn't run, or you haven't restarted Claude Code.

**Fix:**
1. `./scripts/install_commands.sh` (copies to `~/.claude/commands/`).
2. Restart Claude Code.
3. Type `/x` and check autocomplete.

---

## MCP tools don't appear in Claude Desktop

**Cause:** Claude Desktop's MCP config doesn't point at the venv-installed `xsensai-mcp` binary, or you haven't restarted.

**Fix:**
1. Find `~/Library/Application Support/Claude/claude_desktop_config.json`.
2. Make sure it has an `xsensai` server entry pointing at `.venv/bin/xsensai-mcp` (or `python -m xsensai.mcp_server` against the venv's Python).
3. Restart Claude Desktop.
4. From any conversation: "use the xsensai server's ping tool with echo=hello" → should return "pong: hello".

---

## Tests pass but `/xfind` feels wrong

Symptoms: real-corpus `/xfind` returns weird matches, or pinned cards dominate.

**Investigate:**
- Run the F1 quality gate against your real corpus: write a `~/.cache/xsensai/golden_set.json` with `(query, expected_id_list)` tuples and adapt `tests/eval/golden_set.py`. Aim for top-3 hit rate ≥ 80%.
- Inspect scoring constants in `src/xsensai/retrieval/scoring.py`:
  - `RECENCY_HALF_LIFE_DAYS = 90.0` — tighten (smaller value) to favor newer cards more aggressively.
  - `FALLBACK_TOP_SCORE_FLOOR = 0.35` — raise to fire fallback more often.
  - `PIN_DOMINANCE_FRACTION = 0.5` — raise to be stricter about pinned cards.
- If quality is consistently below 80% top-3, the autoplan D1 deferral was wrong: pull Claude/GPT re-rank forward (currently scheduled for Slice 3).
