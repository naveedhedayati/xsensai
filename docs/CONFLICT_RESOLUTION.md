# CONFLICT_RESOLUTION — manually merging cross-host card conflicts

When cron and your Mac both write the same card with different content
(rare, but happens), Slice 5's `git_merge` fails LOUDLY rather than
silently merging the wrong way. You'll see a `[CRON_CONFLICT_UNRESOLVED]`
banner in `/xfind` and find new files in your vault:

```
your-vault/
├── _conflicts.md                              ← committed log of conflicts
├── _conflicts/
│   └── <run-id>/
│       ├── <card-name>.md.local                ← cron's version
│       └── <card-name>.md.remote               ← your Mac's version
│
└── ... (rest of vault) ...
```

The original card path (e.g., `04_areas/x-bookmarks/2026-04-28-foo-12345.md`)
contains whichever version cron incorporated from the remote during the
fail-loud sequence. If the conflict file was on a `.raw.txt` sidecar, the
same `.local`/`.remote` files appear with the `.raw.txt` extension.

This is a manual-resolution workflow. There's no `/xresolve` slash command
in Slice 5; conflicts are rare enough that a documented manual workflow
is sufficient. (If conflicts become routine, file a TODO entry.)

> **Slice 6 update — shadow union log.** Cron now also computes a
> deterministic union-merge candidate per spec line 213-214 and writes a
> log entry to `_conflicts.md` with the diff (`would_have_merged` /
> `would_have_dropped` / byte sizes). The shadow union does NOT change
> the rebase outcome — fail-loud sidecars stay primary in Slice 6. The
> shadow log is for review-and-promote: if you eyeball recent shadow
> entries and the union outcome would have been correct in every case,
> Slice 7+ promotes the union resolver to primary. See
> `~/.claude/plans/immutable-waddling-quokka.md` for the promotion gate.

---

## Worked example

Imagine the card `2026-04-28-claude-fan-7878.md` got conflicted. After
pulling from your vault repo on Mac, you have:

```
04_areas/x-bookmarks/2026-04-28-claude-fan-7878.md         ← upstream version (cron's view)
_conflicts/cron-2026-04-28T07:00:00Z/2026-04-28-claude-fan-7878.md.local
_conflicts/cron-2026-04-28T07:00:00Z/2026-04-28-claude-fan-7878.md.remote
_conflicts.md                                              ← log entry pointing here
```

### Step 1 — diff to see what differs

```bash
cd /path/to/your/vault
diff \
  _conflicts/cron-2026-04-28T07:00:00Z/2026-04-28-claude-fan-7878.md.local \
  _conflicts/cron-2026-04-28T07:00:00Z/2026-04-28-claude-fan-7878.md.remote
```

Or open both in your editor side-by-side. In Obsidian, open both files
and visually compare. Common conflict patterns:

| Frontmatter field | Most likely cause | Resolution heuristic |
|---|---|---|
| `last_seen_at` | Cron and you both saw the tweet at different times | Keep newer |
| `pinned` | You pinned, cron didn't see it yet | Keep `true` |
| `why_saved` | You annotated, cron has stale value | Keep your version |
| `notes:` | You added notes, cron has older notes | Union the arrays |
| `tags`, `applicability` | Both sides edited curation fields | Union (dedupe) |
| `source_status` | Tweet was deleted; only cron knows | Keep cron's `deleted` |

For the body content, generally **prefer the version with more user
intent** — your Mac's annotations almost always win over cron's
auto-generated body.

### Step 2 — write the merged card

Manually merge into the original path:

```bash
# Open both versions, then write the merged result back to the original
$EDITOR 04_areas/x-bookmarks/2026-04-28-claude-fan-7878.md
```

In your editor: take the field-by-field decisions you made above,
write a clean merged version, save.

### Step 3 — clean up `_conflicts/<run-id>/`

Once you've saved the merged card, delete the sidecar dir:

```bash
git rm -rf _conflicts/cron-2026-04-28T07:00:00Z/
```

If the dir is empty after the rm (no other unresolved conflicts), you
can also clean up `_conflicts.md` — leave a one-line note that you
resolved this run:

```bash
# Optional: clean the marker for this run
$EDITOR _conflicts.md
# Add: "_resolved manually 2026-04-28 — see git log for the merge commit_"
```

### Step 4 — commit + push

```bash
git add 04_areas/x-bookmarks/2026-04-28-claude-fan-7878.md _conflicts/ _conflicts.md
git commit -m "cron: resolve cross-host conflict on 2026-04-28-claude-fan-7878"
git push origin main
```

### Step 5 — verify

The next cron run won't re-conflict on this card (because cron's view of
the card is the merged content you just pushed). The `[CRON_CONFLICT_UNRESOLVED]`
banner should clear after the next successful cron run updates the
heartbeat — you can also force a refresh by running `gh workflow run
sync.yml`.

---

## When conflicts pile up

If you see multiple `_conflicts/<run-id>/` dirs because you ignored a
flag for a few weeks:

1. Newest run-id contains the most recent state — resolve that one first.
2. Older run-ids are usually subsumed by newer ones (cron will have
   re-converged after your Mac pushed). You can usually `git rm -rf`
   the older ones without inspecting them.
3. If unsure, diff the older `.local` against the current card; if
   identical, the older conflict is moot.

---

## Why fail-loud, not auto-merge?

Slice 5 deliberately ships fail-loud `.local`/`.remote` sidecars instead
of automatic union-frontmatter merging. Reasoning (per the autoplan
review):

1. **Bilateral logic doesn't generalize.** Slice 6+ adds multi-stream
   sources (paste, mobile, video). Conflicts become 3+-way; bilateral
   union rules don't compose. Better to keep manual resolution stable
   until the topology is clearer.
2. **Cost of a wrong auto-merge is higher than cost of manual review.**
   Auto-merging a `pinned` field wrong, for example, silently demotes
   a card you cared about. Manual review takes 2-3 minutes; demoting
   a pinned card matters much more.
3. **Conflicts are rare in practice.** Same-tweet writes from cron + Mac
   within the same window happen maybe once a month. Manual cost is
   low.

If you find yourself resolving the same kind of conflict repeatedly,
it's worth filing a TODO to add a structured resolver for that field.
