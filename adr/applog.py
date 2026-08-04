"""The service's own log, in a file the web UI can read.

Everything the application says has gone to stderr and from there into
journald. That is the right place for it on a systemd host, and completely
useless from a phone: reading it needs `pct exec` and a shell, which is
exactly what someone looking at the dashboard does not have. Every diagnosis
in this application's history has ended with "paste me the output of
journalctl", and that is a design failure, not a support process.

So the application keeps its own copy. A rotating file beside the database,
written by the service user, readable by the web UI in the same process — no
privileges, no journald group membership, nothing to configure.

Reading is deliberately done backwards from the end of the file. A log is only
ever read from the bottom, and a service that has been running for a month
should not have to be read from the top to show the last hundred lines.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: One file, rotated. Five megabytes is a few days of INFO on a busy machine
#: and still opens instantly; three backups is enough to cover a weekend
#: nobody was watching.
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

LOG_FILENAME = "adr.log"

#: Matches the timestamped line format written below, so a level filter can
#: tell a real log line from a traceback's continuation lines.
_LINE = re.compile(
    r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[(?P<level>[A-Z]+)\] (?P<rest>.*)$",
)

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: How much of the file's tail is read to satisfy a request. Generous enough
#: for a few hundred lines including tracebacks, small enough to be instant.
_TAIL_BYTES = 1024 * 1024


def log_path(config) -> Path:
    """Where the application writes its log."""
    base = getattr(config, "log_path", None)
    if base:
        return Path(base) / LOG_FILENAME
    from adr.config import PROJECT_ROOT
    return Path(PROJECT_ROOT) / "logs" / LOG_FILENAME


def configure(config, level: str | None = None) -> Path | None:
    """Add a rotating file handler to the root logger. Returns the path.

    Best-effort: a container where the log directory cannot be written is a
    problem worth a warning, not worth refusing to start over. The service
    still logs to stderr, which is where it always went.
    """
    path = log_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8",
        )
    except OSError:
        logger.warning("Could not open the log file at %s", path, exc_info=True)
        return None

    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    # Replacing rather than adding: configure() runs once at startup, but a
    # test or a reload calling it twice must not double every line.
    for existing in list(root.handlers):
        if isinstance(existing, logging.handlers.RotatingFileHandler):
            root.removeHandler(existing)
            existing.close()
    if level:
        handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)
    quieten_request_logging()
    return path


#: How often the dashboard asks these, per browser tab, for ever.
#:
#: Five seconds each. The log this application keeps exists to answer "what
#: happened", and a real diagnostic tail read like this:
#:
#:     GET /api/system    GET /api/status    GET /api/preflight
#:     GET /api/system    GET /api/status    GET /api/preflight
#:
#: — a hundred and twenty lines of it, six of them about a disc. The answer
#: was in the file and had been pushed off the end of it by the polling that
#: was meant to be invisible.
POLLED_PATHS = (
    "/api/system", "/api/status", "/api/preflight", "/api/jobs/active",
    "/api/drives/health", "/static/",
    # In-browser playback is one person watching one film and twenty-five
    # range requests. The event is worth nothing to a diagnosis and the lines
    # push everything else off the end of the file.
    "/stream/",
)

#: Statuses that mean "nothing to see". 206 is a range request, which is what
#: playback consists of.
QUIET_STATUSES = (" 200 -", " 206 -", " 304 -")


class _NotJustPolling(logging.Filter):
    """Drops werkzeug's line for a request nobody needs a record of.

    Only the polling and the static files. A POST that changed something, a
    page someone opened, anything that failed — all still logged, because the
    point is to make the interesting lines findable rather than to make the
    log short.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if not any(status in message for status in QUIET_STATUSES):
            return True                      # anything that did not simply work
        return not any(path in message for path in POLLED_PATHS)


def quieten_request_logging() -> None:
    """Keep the dashboard's own polling out of the service log."""
    access = logging.getLogger("werkzeug")
    if not any(isinstance(f, _NotJustPolling) for f in access.filters):
        access.addFilter(_NotJustPolling())


def read_tail(
    config,
    lines: int = 200,
    level: str = "",
    search: str = "",
) -> dict:
    """The end of the log, newest last.

    *level* keeps lines at that level and above, so asking for WARNING does not
    also hide the ERROR you were looking for. A continuation line — the body of
    a traceback — is kept with the line it belongs to, because a traceback
    filtered down to its first line is not a traceback.
    """
    path = log_path(config)
    result = {"path": str(path), "exists": False, "lines": [], "truncated": False}

    try:
        size = path.stat().st_size
    except OSError:
        return result
    result["exists"] = True

    try:
        with open(path, "rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()               # drop the partial first line
                result["truncated"] = True
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        logger.warning("Could not read %s", path, exc_info=True)
        return result

    kept = _filter(text.splitlines(), level, search)
    if len(kept) > lines:
        kept = kept[-lines:]
        result["truncated"] = True
    result["lines"] = kept
    return result


def _filter(raw: list[str], level: str, search: str) -> list[str]:
    """Apply the level and search filters, keeping tracebacks whole."""
    wanted = level.upper() if level.upper() in LEVELS else ""
    threshold = LEVELS.index(wanted) if wanted else 0
    needle = search.lower().strip()

    kept: list[str] = []
    keeping = False
    for line in raw:
        match = _LINE.match(line)
        if match:
            # A new record: decide afresh whether it is wanted.
            try:
                keeping = LEVELS.index(match.group("level")) >= threshold
            except ValueError:
                keeping = not wanted
            if keeping and needle:
                keeping = needle in line.lower()
        elif not kept:
            # Continuation before any record was seen — a traceback whose head
            # fell off the start of the window.
            keeping = not wanted and not needle
        # Continuation lines inherit the decision made for their record.
        if keeping:
            kept.append(line)
    return kept


def describe(config) -> dict:
    """Size and location of the log, for the page header."""
    path = log_path(config)
    info = {"path": str(path), "exists": False, "size_kb": 0, "rotated": 0}
    try:
        info["size_kb"] = round(path.stat().st_size / 1024, 1)
        info["exists"] = True
    except OSError:
        return info
    try:
        with os.scandir(path.parent) as entries:
            info["rotated"] = sum(
                1 for e in entries if e.name.startswith(LOG_FILENAME + ".")
            )
    except OSError:
        pass
    return info
