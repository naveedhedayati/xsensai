"""_sync-status.md heartbeat — written on every /xsync run (success or failure).

Per spec section "_sync-status.md heartbeat (always written)" + /autoplan
D-S3 fix: this file is COMMITTED (not gitignored) so Slice 5 cron's
heartbeat is readable on the user's laptop after a `git pull`.

The companion `_sync-debug.log` (gitignored) is for noisy per-card
diagnostics — separate concern, separate file.

`/xhelp` and `/xfind` read this file to surface the sync-health banner
when `consecutive_failures >= 2` or `last_success > 5 days ago`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from xsensai.storage import sidecar


log = logging.getLogger(__name__)


STATUS_FILE_NAME = "_sync-status.md"

# Banner threshold: spec line 230.
BANNER_FAILURE_THRESHOLD = 2
BANNER_STALE_DAYS = 5

# Slice 5 — cron-only banner thresholds (independent of manual /xsync).
# Cron cadence is every 2 days; 5 days = at least 2 missed runs.
BANNER_CRON_FAILURE_THRESHOLD = 2
BANNER_CRON_STALE_DAYS = 5

# Slice 5 — extraction backlog growing thresholds (per autoplan E5 + Spike #10).
EXTRACTION_BACKLOG_COUNT_THRESHOLD = 50
EXTRACTION_BACKLOG_AGE_DAYS = 30


@dataclass
class SyncStatus:
    """In-memory representation of _sync-status.md.

    Schema matches spec lines 218-227 + Phase 3 additions + Slice 5
    cron-only fields (auto-decision Eng E5).

    Manual /xsync updates `last_run`, `last_success`, `consecutive_failures`.
    Cron updates BOTH the manual fields AND the cron-only mirror fields
    (`last_cron_run`, `last_cron_success`, `consecutive_cron_failures`),
    so a healthy manual /xsync cannot mask a dead cron pipeline.
    """

    last_run: str
    last_success: Optional[str] = None
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    new_cards_this_run: int = 0
    extraction_pending_count: int = 0
    total_cards: int = 0
    threads_permanently_unfetched_this_run: int = 0
    threads_permanently_unfetched_cumulative: int = 0
    # Slice 5 — cron-only mirror (separate from manual). Backwards compatible:
    # pre-Slice-5 status files read these as None / 0.
    last_cron_run: Optional[str] = None
    last_cron_success: Optional[str] = None
    consecutive_cron_failures: int = 0
    last_cron_runner: Optional[str] = None  # "github-actions" | "local"
    oldest_pending_age_days: int = 0

    def to_yaml_frontmatter(self) -> str:
        """Render as the frontmatter-only .md spec format."""
        lines = ["---"]
        lines.append(f"last_run: {self.last_run}")
        lines.append(f"last_success: {self.last_success or 'null'}")
        lines.append(f"consecutive_failures: {self.consecutive_failures}")
        if self.last_error:
            lines.append(f"last_error: {self.last_error!r}")
        else:
            lines.append("last_error: null")
        lines.append(f"new_cards_this_run: {self.new_cards_this_run}")
        lines.append(f"extraction_pending_count: {self.extraction_pending_count}")
        lines.append(f"total_cards: {self.total_cards}")
        lines.append(
            f"threads_permanently_unfetched_this_run: {self.threads_permanently_unfetched_this_run}"
        )
        lines.append(
            f"threads_permanently_unfetched_cumulative: {self.threads_permanently_unfetched_cumulative}"
        )
        # Slice 5 — cron-only fields. Always written so the schema stays
        # forward/backward compatible and human-readable.
        lines.append(f"last_cron_run: {self.last_cron_run or 'null'}")
        lines.append(f"last_cron_success: {self.last_cron_success or 'null'}")
        lines.append(f"consecutive_cron_failures: {self.consecutive_cron_failures}")
        lines.append(f"last_cron_runner: {self.last_cron_runner or 'null'}")
        lines.append(f"oldest_pending_age_days: {self.oldest_pending_age_days}")
        lines.append("---")
        lines.append("")
        lines.append("Heartbeat written by /xsync. Do not edit by hand — `/xhelp` and")
        lines.append("`/xfind` read this file to surface sync-health banner. See ")
        lines.append("[CLAUDE.md](../../CLAUDE.md) Slice 4 section for schema.")
        lines.append("")
        return "\n".join(lines)

    @classmethod
    def from_file(cls, status_path: Path) -> Optional["SyncStatus"]:
        """Read existing status. Returns None if file missing OR parse fails."""
        if not status_path.exists():
            return None
        try:
            text = status_path.read_text(encoding="utf-8")
        except OSError:
            return None

        # Simple frontmatter parse — we control the writer so format is stable.
        if not text.startswith("---"):
            return None
        lines = text.split("\n")
        if len(lines) < 3:
            return None
        # Find closing ---
        end_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx is None:
            return None
        body = lines[1:end_idx]
        kv: dict[str, str] = {}
        for line in body:
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            kv[k.strip()] = v.strip()

        def _opt_str(key: str) -> Optional[str]:
            v = kv.get(key)
            if v is None or v == "null":
                return None
            return v.strip("'\"")

        def _int(key: str, default: int = 0) -> int:
            try:
                return int(kv.get(key, str(default)))
            except (TypeError, ValueError):
                return default

        return cls(
            last_run=kv.get("last_run", ""),
            last_success=_opt_str("last_success"),
            consecutive_failures=_int("consecutive_failures"),
            last_error=_opt_str("last_error"),
            new_cards_this_run=_int("new_cards_this_run"),
            extraction_pending_count=_int("extraction_pending_count"),
            total_cards=_int("total_cards"),
            threads_permanently_unfetched_this_run=_int(
                "threads_permanently_unfetched_this_run"
            ),
            threads_permanently_unfetched_cumulative=_int(
                "threads_permanently_unfetched_cumulative"
            ),
            # Slice 5 — defaults preserve pre-Slice-5 file compat.
            last_cron_run=_opt_str("last_cron_run"),
            last_cron_success=_opt_str("last_cron_success"),
            consecutive_cron_failures=_int("consecutive_cron_failures"),
            last_cron_runner=_opt_str("last_cron_runner"),
            oldest_pending_age_days=_int("oldest_pending_age_days"),
        )

    def should_show_stale_banner(self, *, now: Optional[datetime] = None) -> bool:
        """True if `/xhelp`/`/xfind` should surface a stale-sync banner.

        Triggers on EITHER manual-sync staleness OR cron-only staleness —
        a healthy manual /xsync no longer masks a dead cron pipeline
        (autoplan E5).
        """
        if self.consecutive_failures >= BANNER_FAILURE_THRESHOLD:
            return True
        if self.should_show_cron_stale_banner(now=now):
            return True
        if self.last_success is None:
            return False
        try:
            last = datetime.fromisoformat(self.last_success.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        now = now or datetime.now(timezone.utc)
        return (now - last) > timedelta(days=BANNER_STALE_DAYS)

    def should_show_cron_stale_banner(
        self, *, now: Optional[datetime] = None
    ) -> bool:
        """True if cron-only health is bad — independent of manual /xsync.

        Per autoplan E5: a successful manual /xsync must NEVER reset
        cron-only counters; otherwise the banner is masked. This method
        fires when:
          - `consecutive_cron_failures >= BANNER_CRON_FAILURE_THRESHOLD`, OR
          - `last_cron_run > BANNER_CRON_STALE_DAYS` ago, OR
          - `last_cron_run is None` AND any sync activity exists (cron
            never ran but manual /xsync did — likely user hasn't set
            up cron yet; informational not error).
        """
        if self.consecutive_cron_failures >= BANNER_CRON_FAILURE_THRESHOLD:
            return True
        if self.last_cron_run is None:
            return False  # never-fired-yet is a setup hint, not staleness
        try:
            last = datetime.fromisoformat(
                self.last_cron_run.replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return False
        now = now or datetime.now(timezone.utc)
        return (now - last) > timedelta(days=BANNER_CRON_STALE_DAYS)

    def should_show_extraction_backlog_banner(self) -> bool:
        """True if the extraction backlog is past the EXTRACTION_BACKLOG
        thresholds (count >= 50 OR oldest >= 30 days). Surfaces
        [INFO/EXTRACTION_BACKLOG_GROWING] in /xfind, /xhelp, /xask.

        Spike #10 finding: body-only retrieval drops top-3 hit rate by
        ~27pp vs body+tags+summary. Backlog growing = silent /xfind
        quality decay; this banner is the warning.
        """
        if self.extraction_pending_count >= EXTRACTION_BACKLOG_COUNT_THRESHOLD:
            return True
        if self.oldest_pending_age_days >= EXTRACTION_BACKLOG_AGE_DAYS:
            return True
        return False

    def cron_never_fired(self) -> bool:
        """True if cron has never run on this corpus.

        Used by /xhelp to show a one-time "cron is configured but has
        never fired" hint that points the user at docs/CRON_SETUP.md.
        """
        return self.last_cron_run is None


def write_status(corpus_path: Path, status: SyncStatus) -> Path:
    """Write _sync-status.md atomically (rename-based)."""
    status_path = corpus_path / STATUS_FILE_NAME
    sidecar.durable_replace(
        status_path,
        status.to_yaml_frontmatter().encode("utf-8"),
        durability="metadata",
    )
    return status_path


def read_status(corpus_path: Path) -> Optional[SyncStatus]:
    """Convenience reader — returns None if no status file exists yet."""
    return SyncStatus.from_file(corpus_path / STATUS_FILE_NAME)


def update_after_run(
    corpus_path: Path,
    *,
    success: bool,
    new_cards_this_run: int,
    extraction_pending_count: int,
    total_cards: int,
    threads_unfetched_this_run: int = 0,
    last_error: Optional[str] = None,
    now: Optional[datetime] = None,
    cron_runner: Optional[str] = None,
    oldest_pending_age_days: int = 0,
) -> SyncStatus:
    """Read existing status, update counters, write back.

    Slice 5: when `cron_runner` is non-None, mirror the run into
    cron-only counters (`last_cron_run`, `last_cron_success`,
    `consecutive_cron_failures`). Manual /xsync passes
    `cron_runner=None` and never touches the cron-only counters
    (autoplan E5 — prevents manual sync from masking dead cron).
    """
    now = now or datetime.now(timezone.utc)
    iso_now = now.isoformat()
    prior = read_status(corpus_path)
    consecutive = (prior.consecutive_failures if prior else 0)
    cumulative_unfetched = (prior.threads_permanently_unfetched_cumulative if prior else 0)

    if success:
        last_success = iso_now
        consecutive = 0
    else:
        last_success = prior.last_success if prior else None
        consecutive += 1

    # Cron-only mirror — never reset by manual /xsync (autoplan E5).
    last_cron_run = prior.last_cron_run if prior else None
    last_cron_success = prior.last_cron_success if prior else None
    consecutive_cron = prior.consecutive_cron_failures if prior else 0
    last_cron_runner = prior.last_cron_runner if prior else None

    if cron_runner is not None:
        last_cron_run = iso_now
        last_cron_runner = cron_runner
        if success:
            last_cron_success = iso_now
            consecutive_cron = 0
        else:
            consecutive_cron += 1

    new_status = SyncStatus(
        last_run=iso_now,
        last_success=last_success,
        consecutive_failures=consecutive,
        last_error=last_error,
        new_cards_this_run=new_cards_this_run,
        extraction_pending_count=extraction_pending_count,
        total_cards=total_cards,
        threads_permanently_unfetched_this_run=threads_unfetched_this_run,
        threads_permanently_unfetched_cumulative=cumulative_unfetched + threads_unfetched_this_run,
        last_cron_run=last_cron_run,
        last_cron_success=last_cron_success,
        consecutive_cron_failures=consecutive_cron,
        last_cron_runner=last_cron_runner,
        oldest_pending_age_days=oldest_pending_age_days,
    )
    write_status(corpus_path, new_status)
    return new_status


__all__ = [
    "SyncStatus",
    "write_status",
    "read_status",
    "update_after_run",
    "STATUS_FILE_NAME",
    "BANNER_FAILURE_THRESHOLD",
    "BANNER_STALE_DAYS",
    "BANNER_CRON_FAILURE_THRESHOLD",
    "BANNER_CRON_STALE_DAYS",
    "EXTRACTION_BACKLOG_COUNT_THRESHOLD",
    "EXTRACTION_BACKLOG_AGE_DAYS",
]
