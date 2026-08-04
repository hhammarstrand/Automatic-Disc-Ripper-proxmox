"""Deleting a job's files, when that is what was asked for.

Removing a row from the history and removing a film from the library are two
different acts, and conflating them is the kind of mistake nobody gets to undo.
So this module exists to make the second one explicit: it is never implied by
the first, it says exactly which files it would remove *before* removing them,
and it is deliberately narrow about what counts as "the job's files".

The narrowness is the point. A job knows where it put things — the tracks'
output paths, the job's output folder, its Plex folder, the raw directory —
and only those are candidates. Nothing walks a tree looking for likely-looking
video; nothing deletes a directory that still has something else in it. A
library folder shared with files this job never produced comes out of a delete
with those files intact.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def job_files(job, config) -> list[Path]:
    """Every file this job produced that is still on disk.

    Ordered and de-duplicated, so it can be shown to someone before they say
    yes. A delete that cannot be previewed is a delete nobody can consent to.
    """
    from adr.naming import finished_files

    found: list[Path] = []

    def add(path) -> None:
        if not path:
            return
        candidate = Path(str(path))
        if candidate.is_file() and candidate not in found:
            found.append(candidate)

    # What the tracks recorded is the most precise answer: those paths were
    # written by the encoder, not inferred afterwards.
    for track in getattr(job, "tracks", []) or []:
        add(getattr(track, "output_path", None))

    # And the folders the job used, for anything the tracks did not record —
    # a job interrupted before its rows were updated, or an older schema.
    for folder in (getattr(job, "output_path", None), getattr(job, "plex_path", None)):
        if folder:
            for path in finished_files(folder):
                add(path)

    return found


def raw_files(job, config) -> list[Path]:
    """The untranscoded rip, if the cleanup has not already run."""
    raw_dir = Path(config.raw_path) / str(job.id)
    if not raw_dir.is_dir():
        return []
    try:
        return sorted(p for p in raw_dir.iterdir() if p.is_file())
    except OSError:
        return []


def human_size(count: int) -> str:
    """A byte count someone can judge at a glance.

    In megabytes throughout, a film reads as "4823.7 MB" and a stray subtitle
    as "0.0 MB" — the first is hard to weigh and the second looks like nothing
    worth keeping, which is exactly the wrong impression to give in a delete
    confirmation.
    """
    size = float(count)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def describe(job, config) -> dict:
    """What deleting this job's files would remove. Nothing is deleted here."""
    finished = job_files(job, config)
    raw = raw_files(job, config)
    total = sum(_size(p) for p in finished + raw)
    return {
        "files": [str(p) for p in finished],
        "raw": [str(p) for p in raw],
        "bytes": total,
        "size": human_size(total),
    }


def delete_job_files(job, config) -> dict:
    """Delete this job's output and raw files. ``{"deleted", "failed"}``.

    Never raises on a file it cannot remove: a partial delete reported
    honestly is more use than an exception halfway through, which leaves the
    caller unable to say what happened.
    """
    deleted: list[str] = []
    failed: list[str] = []
    directories: set[Path] = set()

    for path in job_files(job, config) + raw_files(job, config):
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path, exc)
            failed.append(f"{path}: {exc}")
            continue
        deleted.append(str(path))
        directories.add(path.parent)

    _remove_empty(directories, config)
    return {"deleted": deleted, "failed": failed}


def _remove_empty(directories: set[Path], config) -> None:
    """Take away the folders the deleted files were the only occupants of.

    Only when empty, and never the configured roots themselves. A film's
    folder left behind after its film is gone is litter; the library folder
    removed because it happened to be empty is a bug with a very bad day
    attached to it.
    """
    protected = {
        Path(str(p)).resolve()
        for p in (
            config.completed_path, config.raw_path, config.staging_path,
            getattr(config, "plex_path", "") or None,
            getattr(config, "tv_path", "") or None,
        )
        if p
    }

    for directory in sorted(directories, key=lambda d: len(d.parts), reverse=True):
        try:
            if directory.resolve() in protected:
                continue
            if any(directory.iterdir()):
                continue
            directory.rmdir()
        except OSError:
            logger.debug("Left %s in place", directory, exc_info=True)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
