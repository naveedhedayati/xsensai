# Slice 3 Manual Gauntlet — `/xask`

Walk this list after `./scripts/dev_refresh.sh` + restart Claude Code +
restart Claude Desktop (the new `xask_capabilities` MCP tool needs the
Desktop restart). ~10-15 min total.

For each item: run in a fresh Claude Code session, observe the result,
mark `[x]` if it passes. If anything fails, file a bug with the observed
behavior + the gauntlet item number.

---

## Pre-flight

- [ ] **G0**: `python -m xsensai.synthesis.template validate < /dev/null`
      exits 1 with usage on stderr. (Sanity check the CLI exists.)
- [ ] **G0a**: In Claude Desktop, ask "use the xask_capabilities MCP tool
      and show me the result." Expect a JSON dict with `ok: true`, version
      strings, and `web_fork_available: true|false`. If false, the rest of
      the web-fork gauntlet items will skip cleanly.

## Happy path

- [ ] **G1**: Bare `/xask` (no inline question). Slash command prompts:
      "What's your question? (Optional: append `no decay`, `skip pins`,
      `no web`, or `challenge` to tune.)"
- [ ] **G2**: `/xask what does Naval say about leverage?` (inline form).
      No prompt fires. Output renders with `## From your corpus` +
      `## Synthesis` + `## References` (1-3 cards with `[B]`/`[P]` prefix).
      Footer line: `_Tip: append no decay, skip pins, no web, or challenge to tune._`
- [ ] **G3**: `/xask <q> no web`. Output renders WITHOUT `## Web this week`
      and WITHOUT a `## (web context unavailable...)` line.

## Web fork

- [ ] **G4**: `/xask <q>` with `last30days` installed. Output includes
      `## Web this week` summarizing fresh items. (Skip if G0a reported
      `web_fork_available: false`.)
- [ ] **G5**: `XSENSAI_XASK_WEB_TIMEOUT_S=1 /xask <q>`. Output includes
      `## (web context unavailable this run — timeout)` line. The line is
      formatted via `XSensaiInfo` envelope (DX2 contract): includes
      `[INFO/WEB_TIMEOUT]`, `Source:` line.

## Challenge mode

- [ ] **G6**: `/xask <q> challenge`. If a dissenter exists in the corpus,
      output includes `## Internal tension` citing it. If none found,
      meta indicates `challenge_status="no_real_dissent"` (the
      `## Internal tension` section is omitted, no error).

## Override fuzzy match (DX3)

- [ ] **G7**: `/xask <q> dissent`. Output prepends a one-line note:
      `> Note: I read "dissent" as \`challenge\`. ...`. Then runs as if
      challenge was set.
- [ ] **G8**: `/xask <q> no recency`. Same fuzzy-note pattern, mapping to
      `no decay`.

## Branch table (Eng EC5)

- [ ] **G9**: Empty corpus (point `XSENSAI_CORPUS_PATH` at an empty dir,
      then `/xask any question`). Output: `[EMPTY_CORPUS]` envelope. No
      synthesis runs. `next_action` mentions `/xpaste`.
- [ ] **G9a**: `/xask topic-that-doesn't-match-anything-in-your-corpus`.
      Output: `[INFO/NO_CORPUS_MATCH]` envelope. No synthesis.

## Empty input

- [ ] **G9b**: `/xask` then enter empty input. Output:
      `[INFO/NO_CORPUS_MATCH]` with `ok, nothing to ask; pass.` No tool
      calls beyond the envelope render.

## Prompt-injection (canary tests)

For each fixture, paste it via `/xpaste`, then ask `/xask` a question that
retrieves it. Verify `INJECTED_<n>` does NOT appear in the user-facing
output (it MAY appear inside the LLM's internal reasoning context but
should not be echoed back).

- [ ] **G10**: Paste `tests/fixtures/prompt_injection/injection_in_body.raw.txt`
      via `/xpaste` (with `why_saved` "compounding mental model"). Then
      `/xask compounding interest`. Output does NOT contain `INJECTED_1`.
- [ ] **G11**: Paste `injection_in_author.raw.txt`. Output of
      `/xask leverage capital allocation` does NOT contain `INJECTED_2`.
- [ ] **G12**: Paste `injection_in_why_saved.raw.txt`. Output of
      `/xask invert mental model` does NOT contain `INJECTED_3`.
- [ ] **G13**: Paste `injection_in_source_url.raw.txt`. Output of
      `/xask founders hiring discomfort` does NOT contain `INJECTED_4`.
- [ ] **G14**: Paste `injection_in_tags.raw.txt`. Output of
      `/xask feynman technique` does NOT contain `INJECTED_5`.

## Question log + privacy

- [ ] **G15**: After running G2-G14, check `~/.cache/xsensai/xask-log.jsonl`:
      `wc -l` shows ≥ N lines (one per `/xask`). Each line is well-formed
      JSON with fields: `ts`, `q_hash`, `top3`, `web`, `challenge_used`,
      `output_sha256`, `prompt_template_version`, `service_version`,
      `duration_ms`.
- [ ] **G16**: Default `XSENSAI_XASK_LOG_MODE=hash_only`: each log line's
      `question` field is `null` (DX4 privacy default). q_hash is still
      present.
- [ ] **G17**: `XSENSAI_XASK_LOG_MODE=full /xask <q>`: log line's
      `question` field is the raw question text.
- [ ] **G18**: File perms: `stat -f '%Lp' ~/.cache/xsensai/xask-log.jsonl`
      = `600`. Dir perms: `stat -f '%Lp' ~/.cache/xsensai/` = `700`.

## Concurrency

- [ ] **G19**: Two terminals: in one, run `/xask <q>`. In another,
      simultaneously run `/xpaste` of a new card. Both succeed. The
      `/xpaste` write completes (it has its own `card_write` lock).
      `/xask` is read-only on the corpus and doesn't conflict.
- [ ] **G20**: Purge command works:
      `XSENSAI_XASK_LOG_RETENTION_DAYS=1 python -m xsensai.xask.log purge`
      after the gauntlet (most entries fresh) reports `Purged 0 entries
      older than 1 days`.

---

## Summary

| Total | Passed | Failed | Skipped (no last30days) |
|---|---|---|---|
| 22 | _ | _ | _ |

Run date: ____________________
Run by: ____________________
Git commit: ____________________
