"""Slice 4 — XDK ingestion via /xsync (manual) + /xextract (batch extraction).

Designed per /autoplan UC-1=C: ship only the manual entry point, but design
extraction.py + lock semantics to be headless-runnable so Slice 5 cron =
"wire up the schedule," not "rewrite the orchestrator."

Public surface (callers should reach for these):
  - sync.client.XClient          — XDK wrapper with OAuth, bookmarks, threads
  - sync.dedup.existing_source_ids — set of source_ids already on disk
  - sync.checkpoint.CheckpointFile — append-on-write resume state
  - sync.heartbeat.write_status   — _sync-status.md heartbeat
  - sync.card_writer.write_one    — single XDK bookmark → v2 card
  - sync.auth.TokenProvider       — abstract token source (Keychain/env)
"""

from xsensai.sync.version import SYNC_SCHEMA_VERSION

__all__ = ["SYNC_SCHEMA_VERSION"]
