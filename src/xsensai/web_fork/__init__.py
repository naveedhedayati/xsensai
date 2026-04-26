"""Web fork package — last30days subprocess orchestration.

Slice 3 only. Wraps the external `last30days` Claude Code skill as a
subprocess so /xask can pull this-week web context in parallel with
corpus retrieval. Env-scrubbed and path-validated per Eng review EC6.
"""

from xsensai.web_fork.last30days_runner import run_last30days

__all__ = ["run_last30days"]
