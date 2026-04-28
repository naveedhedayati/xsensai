"""Headless entrypoints for non-Claude-Code runs (Slice 5 cron + cli).

Currently:
  - `headless`: GH Actions cron orchestrator. Reads env, runs sync,
    commits + pushes the vault repo, exits with structured codes.
"""
