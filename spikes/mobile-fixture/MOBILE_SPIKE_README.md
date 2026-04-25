# Mobile Spike #1 — Mobile Claude Code slash-command discovery

**Goal (~5 min):** find out whether mobile Claude Code auto-discovers
project-local slash commands (defined in `.claude/commands/`). Result
shapes Slice 2/3: if mobile sees slash commands, mobile gets the full
8-command surface; if not, mobile is MCP-only.

## What's here

```
mobile-fixture/
└── .claude/
    └── commands/
        └── hello.md      # one trivial slash command
```

That's it. A throwaway "open this folder as a project on mobile, see if
the slash command shows up" test.

## Procedure

### Option A: standalone repo (cleanest)

1. On your Mac, create a fresh empty repo on GitHub:
   `gh repo create xsensai-mobile-spike --private`
2. Copy this folder's contents (everything inside `mobile-fixture/`) into
   that new repo. Push.
3. On your phone (iOS), open Working Copy or git tools of your choice and
   clone the repo.
4. Open mobile Claude Code. Add the cloned repo as a project / open it
   as the project root.
5. Try invoking `/hello` in a new conversation.
6. Report back:
   - Did `/hello` autocomplete from the slash menu?
   - Did invoking it work and produce the expected response?
   - If autocomplete failed, did typing `/hello` work anyway?

### Option B: nested in this project

If easier: clone the parent `xsensai` repo on your phone, then in mobile
Claude Code try opening `spikes/mobile-fixture/` as the project root
directly. Some clients support subdirectory roots; if yours doesn't,
fall back to Option A.

## Result reporting

Three signals to report back:

1. **Discovery** — does the slash menu show `/hello`?
2. **Invocation** — does typing `/hello` (with or without autocomplete)
   work and respond?
3. **Any friction** — anything weird about how mobile rendered the
   command, args, or response?

Three quick lines is enough; no formal writeup.

## What this means for v2

| Discovery + invocation | Implication |
|---|---|
| Both work | Mobile gets the full 8-command slash surface in Slice 2+ |
| Only invocation works | Slash commands work, but you'll memorize names — usable but slightly worse UX |
| Neither | Mobile is MCP-only. Spec's "mobile bonus" caveat applies — desktop-first, mobile via search_bookmarks / ask_bookmarks tool calls only |
