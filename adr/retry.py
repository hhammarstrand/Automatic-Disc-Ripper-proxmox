"""Resume a failed job from the furthest point that still has its files.

A rip is forty minutes and several gigabytes. Losing all of it because the NAS
was unmounted, or the preset name had a typo, is the difference between an
annoyance and having to find the disc again.

Most failures happen *after* the expensive part:

* the transfer to the destination failed — the encoded film is intact in
  staging, and only the last step needs redoing;
* the encode failed — the raw MKVs are intact, and only the encode needs
  redoing;
* the rip itself failed — nothing survives, and the disc has to go back in.

So retry is not one operation. This module works out which of the three a given
job is in by looking at what is actually on disk, rather than trusting the
status, and reports honestly when nothing can be salvaged.
"""

import logging
from pathlib import Path

from adr.models import JobStatus, Track, TrackStatus

logger = logging.getLogger(__name__)

# What a retry would do. Ordered from cheapest to most expensive.
RESUME_TRANSFER = "transfer"
RESUME_ENCODE = "encode"
RESUME_IMPOSSIBLE = "impossible"


def _encoded_files(job) -> list[Path]:
    """Finished MP4s still sitting where the encoder left them."""
    if not job.output_path:
        return []
    directory = Path(job.output_path)
    if not directory.is_dir():
        return []
    try:
        return sorted(p for p in directory.glob("*.mp4") if p.is_file())
    except OSError:
        return []


def _raw_files(job, config) -> list[Path]:
    """Raw MKVs from the rip, if the cleanup has not run."""
    raw_dir = Path(config.raw_path) / str(job.id)
    if not raw_dir.is_dir():
        return []
    try:
        return sorted(p for p in raw_dir.glob("*.mkv") if p.is_file())
    except OSError:
        return []


def plan(job, config) -> dict:
    """Work out what retrying this job would actually do.

    Returns ``{"resume", "reason", "files", "can_retry"}``. Deliberately based
    on what is on disk: a job marked ERROR during transfer still has a complete
    encode, and a job marked DONE whose files were deleted has nothing.
    """
    if job.status not in (JobStatus.ERROR, JobStatus.CANCELLED):
        return {
            "resume": RESUME_IMPOSSIBLE,
            "reason": "Only failed or cancelled jobs can be retried.",
            "files": [],
            "can_retry": False,
        }

    encoded = _encoded_files(job)
    if encoded:
        return {
            "resume": RESUME_TRANSFER,
            "reason": (
                f"{len(encoded)} encoded file(s) are intact in {job.output_path}. "
                "Retrying moves them to the destination — no re-encoding."
            ),
            "files": [str(p) for p in encoded],
            "can_retry": True,
        }

    raw = _raw_files(job, config)
    if raw:
        return {
            "resume": RESUME_ENCODE,
            "reason": (
                f"{len(raw)} raw MKV(s) from the rip are still on disk. "
                "Retrying re-encodes them — the disc is not needed."
            ),
            "files": [str(p) for p in raw],
            "can_retry": True,
        }

    return {
        "resume": RESUME_IMPOSSIBLE,
        "reason": (
            "Neither the encoded files nor the raw MKVs are still on disk, so "
            "there is nothing to resume from. Put the disc back in to rip it again."
        ),
        "files": [],
        "can_retry": False,
    }


def requeue_encode(job, session, config, encode_queue) -> int:
    """Re-queue every raw MKV for encoding. Returns how many tasks were queued.

    Fresh Track rows are created rather than reusing the old ones: the previous
    attempt's rows carry its error state and output paths, and a retry that
    silently inherits them is hard to reason about afterwards.
    """
    from adr.pipeline import EncodeTask, final_destination
    from adr.storage import should_stage
    from adr.utils import BYTES_PER_MB, make_plex_folder_name, sanitize_filename, unique_output_dir

    raw = _raw_files(job, config)
    if not raw:
        return 0

    for track in list(job.tracks):
        session.delete(track)
    session.commit()

    plex_folder_name = make_plex_folder_name(
        sanitize_filename(job.title or job.disc_label or f"Job {job.id}"), job.year,
    )
    dest_parent, _ = final_destination(job, config)
    staging = should_stage(dest_parent, config.stage_locally)
    if staging:
        final_dir = dest_parent
        output_dir = unique_output_dir(Path(config.staging_path) / plex_folder_name)
    else:
        final_dir = None
        output_dir = unique_output_dir(dest_parent / plex_folder_name)

    job.output_path = str(output_dir)
    job.status = JobStatus.ENCODING
    job.error_message = None
    job.completed_at = None
    job.progress_encode = 0.0
    session.commit()

    for index, mkv in enumerate(raw):
        track = Track(
            job_id=job.id,
            track_number=index + 1,
            filename=mkv.name,
            size_mb=mkv.stat().st_size / BYTES_PER_MB,
            status=TrackStatus.PENDING,
        )
        session.add(track)
        session.commit()

        out_name = plex_folder_name if len(raw) == 1 else f"{plex_folder_name} - pt{index + 1}"
        encode_queue.put(EncodeTask(
            job_id=job.id,
            track_id=track.id,
            input_path=mkv,
            output_dir=output_dir,
            output_filename=out_name,
            final_dir=final_dir,
        ))

    logger.info("Job %s re-queued for encoding (%d file(s))", job.id, len(raw))
    return len(raw)


def retry_transfer(job, session, config) -> tuple[bool, str]:
    """Redo just the move to the destination. Returns ``(ok, message)``.

    The destination is re-checked first: retrying into the same unmounted share
    that caused the failure would fail identically, and saying so up front is
    more use than a second identical error twenty seconds later.
    """
    from adr.pipeline import final_destination, move_to_plex, transfer_to_destination
    from adr.storage import check_destination

    dest_parent, _ = final_destination(job, config)
    ok, error = check_destination(dest_parent, require_mount=config.require_completed_mount)
    if not ok:
        return False, f"The destination still is not usable: {error}"

    if not transfer_to_destination(job, session, dest_parent):
        return False, job.error_message or "The transfer failed again."

    move_to_plex(job, session, config)
    job.status = JobStatus.DONE
    job.error_message = None
    session.commit()
    logger.info("Job %s retried: transfer completed to %s", job.id, dest_parent)
    return True, f"Moved to {job.output_path}."
