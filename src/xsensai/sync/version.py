"""Sync schema version — bump when the on-disk shape of sync state files
(_sync-checkpoint.jsonl, _sync-status.md) changes incompatibly.

Used by /xsync prepare to write into checkpoint records and by future
recovery code that needs to handle older formats.
"""

SYNC_SCHEMA_VERSION = "1.0.0"
