"""A log per job, so a failure is diagnosable without SSH.

When a rip fails the UI shows one error string. What actually went wrong — the
MakeMKV message about a read error on title 3, HandBrake's complaint about the
preset — is in the journal, behind `pct exec` and `journalctl -u adr`, mixed in
with every other job that ran that week.

Each job therefore gets its own file. The rules that matter:

* **Bounded.** A pathological disc can make MakeMKV emit tens of thousands of
  lines. The file is capped and keeps the *end*, because the last thing before
  the failure is the interesting part.
* **Never fatal.** Logging is a convenience wrapped around the actual work. A
  full disk must not turn a successful rip into a failed one, so every write is
  suppressed on error.
* **Cleaned up.** Logs are deleted alongside the job they belong to, and swept
  by age, or they accumulate for ever in the container.
"""

import contextlib
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Enough to see a failure in context, small enough that a hundred of them are
# still nothing next to one MKV.
MAX_BYTES = 256 * 1024
# Read back at most this much, so a browser is never handed the whole file.
TAIL_BYTES = 64 * 1024
DEFAULT_RETENTION_DAYS = 30

_NAME_RE = re.compile(r"^job-(\d+)\.log$")


def log_dir(config) -> Path:
    """Where job logs live: alongside the database, on the container's own disk.

    Deliberately not under completed_path — that may be a NAS, and writing a
    line at a time across the network for every progress message would be a
    silly thing to do to a share.
    """
    base = getattr(config, "log_path", None)
    if base:
        return Path(base)
    from adr.config import PROJECT_ROOT
    return Path(PROJECT_ROOT) / "logs"


def log_path(config, job_id: int) -> Path:
    return log_dir(config) / f"job-{int(job_id)}.log"


class JobLog:
    """Append-only log for one job. Every method is best-effort by design."""

    def __init__(self, config, job_id: int):
        self._path = log_path(config, job_id)
        self._job_id = int(job_id)
        self._failed = False

    @property
    def path(self) -> Path:
        return self._path

    def append(self, stage: str, message: str) -> None:
        """Add one line. Silently does nothing if it cannot."""
        if self._failed or not message:
            return
        line = f"{time.strftime('%H:%M:%S')} [{stage}] {message.rstrip()}\n"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line)
            self._trim()
        except OSError as exc:
            # Once. Logging that we cannot log, on every line, would be worse
            # than the original problem.
            self._failed = True
            logger.warning("Job log for %s unavailable: %s", self._job_id, exc)

    def sink(self, stage: str):
        """A one-argument callable for ripper/encoder to hand lines to."""
        return lambda message: self.append(stage, message)

    def _trim(self) -> None:
        """Keep the tail when the file outgrows MAX_BYTES.

        Rewrites rather than truncating in place: the end is what matters, and
        cutting the front mid-line would leave a garbled first entry.
        """
        with contextlib.suppress(OSError):
            if self._path.stat().st_size <= MAX_BYTES:
                return
            with open(self._path, "rb") as fh:
                fh.seek(-MAX_BYTES // 2, os.SEEK_END)
                fh.readline()          # discard the partial line
                keep = fh.read()
            with open(self._path, "wb") as fh:
                fh.write(b"[... earlier output trimmed ...]\n")
                fh.write(keep)


def read(config, job_id: int, tail_bytes: int = TAIL_BYTES) -> str:
    """The end of a job's log, or an empty string if there is none.

    A log longer than *tail_bytes* is announced as such. Handing the UI a log
    that silently begins mid-stream invites reading the first visible line as
    the first thing that happened.
    """
    path = log_path(config, job_id)
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            truncated = size > tail_bytes
            fh.seek(max(0, size - tail_bytes))
            if truncated:
                fh.readline()          # discard the partial first line
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""

    if truncated:
        return f"[... showing the last {tail_bytes // 1024} KB of a longer log ...]\n{text}"
    return text


def delete(config, job_id: int) -> bool:
    """Remove a job's log. Returns whether a file was there to remove."""
    path = log_path(config, job_id)
    try:
        path.unlink()
        return True
    except OSError:
        return False


def prune(config, keep_job_ids: set[int] | None = None,
          max_age_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete orphaned and expired logs. Returns how many went.

    *keep_job_ids* is the set of jobs still in the database; anything else is a
    log for a job the user has already deleted from history.
    """
    directory = log_dir(config)
    if not directory.exists():
        return 0

    cutoff = time.time() - max_age_days * 86400
    removed = 0
    with contextlib.suppress(OSError):
        for entry in directory.iterdir():
            match = _NAME_RE.match(entry.name)
            if not match:
                continue
            job_id = int(match.group(1))
            orphaned = keep_job_ids is not None and job_id not in keep_job_ids
            expired = False
            with contextlib.suppress(OSError):
                expired = entry.stat().st_mtime < cutoff
            if orphaned or expired:
                with contextlib.suppress(OSError):
                    entry.unlink()
                    removed += 1
    if removed:
        logger.info("Pruned %d job log(s)", removed)
    return removed
