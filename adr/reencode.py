"""Encode a finished job again, with the settings as they are now.

The reason to want this is that the settings changed. A different encoder, a
different spoken language, a different quality — and the film sitting in the
library was made under the old ones. Re-ripping the disc to get the new ones
means finding the disc and waiting forty minutes for bytes that are already on
the machine.

Where it encodes *from* is the whole question, and there are two answers with
very different consequences:

* **the raw rip**, when the cleanup has not taken it. Lossless, exactly what a
  first encode would have used, and indistinguishable in the result.
* **the finished file**, when it has. This works and it is second-generation:
  encoding an encode loses a little more each time. Worth doing when the
  change is worth it — a language fix, a container change — and worth saying
  out loud rather than glossing, because "re-encode" sounds free and this
  version of it is not.

Which one applies is decided from what is on disk and reported before anything
runs, so the choice belongs to the person making it.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SOURCE_RAW = "raw"
SOURCE_FINISHED = "finished"
SOURCE_NONE = "none"


def plan(job, config) -> dict:
    """What re-encoding this job would do. ``{"can_reencode", "source", …}``."""
    from adr import cleanup, retry

    raw = retry._raw_files(job, config)
    if raw and job.rip_completed_at is not None:
        return {
            "can_reencode": True,
            "source": SOURCE_RAW,
            "files": [str(p) for p in raw],
            "reason": (
                f"{len(raw)} raw file(s) from the rip are still on disk, so "
                "this re-encodes from them — the same source a first encode "
                "would have used, and the disc is not needed."
            ),
        }

    if raw:
        # Present but from a rip that never finished: truncated in the middle
        # of a frame, and no encoder can do anything with that.
        return {
            "can_reencode": False,
            "source": SOURCE_NONE,
            "files": [],
            "reason": (
                "The rip that produced these files never finished, so they are "
                "incomplete and cannot be encoded. Put the disc back in and "
                "press Rip."
            ),
        }

    finished = cleanup.job_files(job, config)
    if finished:
        return {
            "can_reencode": True,
            "source": SOURCE_FINISHED,
            "files": [str(p) for p in finished],
            "reason": (
                "The raw rip is gone, so this re-encodes the finished file. "
                "That works, and it is a second-generation encode: some "
                "quality is lost that re-ripping the disc would keep. Worth it "
                "for a language or container change, less so for a small "
                "quality adjustment."
            ),
        }

    return {
        "can_reencode": False,
        "source": SOURCE_NONE,
        "files": [],
        "reason": (
            "Neither the raw rip nor the finished file is still on disk, so "
            "there is nothing to encode from. Put the disc back in and press "
            "Rip."
        ),
    }


def start(job, session, config, encode_queue) -> int:
    """Queue the re-encode. Returns how many tasks were queued."""
    from adr import retry

    decision = plan(job, config)
    if not decision["can_reencode"]:
        return 0

    if decision["source"] == SOURCE_RAW:
        # Identical to a retry from raw, and deliberately the same code: two
        # implementations of "encode these files into this job" would drift,
        # and the naming rules alone are worth not duplicating.
        return retry.requeue_encode(job, session, config, encode_queue)

    return _requeue_finished(job, session, config, encode_queue,
                             [Path(p) for p in decision["files"]])


def _requeue_finished(job, session, config, encode_queue, sources: list[Path]) -> int:
    """Encode the finished files again, into a fresh output folder.

    A fresh folder rather than the old one, because the encoder would
    otherwise be reading a file it is in the middle of overwriting. The
    transfer step moves the result into place afterwards, as it does for any
    other encode — this is not a special case once the input is chosen.
    """
    from adr.models import JobStatus, Track, TrackStatus
    from adr.naming import plan_output
    from adr.pipeline import EncodeTask, final_destination
    from adr.storage import should_stage
    from adr.utils import BYTES_PER_MB, unique_output_dir

    for track in list(job.tracks):
        session.delete(track)
    session.commit()

    naming = plan_output(job, len(sources),
                         fallback_title=job.disc_label or f"Job {job.id}")
    dest_parent, _ = final_destination(job, config)
    staging = should_stage(dest_parent, config.stage_locally)
    if staging:
        final_dir = dest_parent
        output_dir = unique_output_dir(Path(config.staging_path) / naming.folder)
    else:
        final_dir = None
        output_dir = unique_output_dir(Path(dest_parent) / naming.folder)

    job.output_path = str(output_dir)
    job.status = JobStatus.ENCODING
    job.error_message = None
    job.completed_at = None
    job.progress_encode = 0.0
    session.commit()

    queued = 0
    for index, source in enumerate(sources):
        try:
            size_mb = source.stat().st_size / BYTES_PER_MB
        except OSError:
            size_mb = None
        track = Track(
            job_id=job.id, track_number=index + 1, filename=source.name,
            size_mb=size_mb, status=TrackStatus.PENDING,
        )
        session.add(track)
        session.commit()
        if naming.episodes and index < len(naming.episodes):
            track.episode_number = naming.episodes[index]
            session.commit()

        encode_queue.put(EncodeTask(
            job_id=job.id,
            track_id=track.id,
            input_path=source,
            output_dir=output_dir,
            output_filename=(
                naming.filenames[index] if index < len(naming.filenames)
                else f"{naming.folder} - pt{index + 1}"
            ),
            final_dir=final_dir,
            # Never passthrough. "Re-encode" that copies the file unchanged
            # would report success and change nothing, which is the worst
            # possible reading of the button.
        ))
        queued += 1

    logger.info("Re-encoding job %s from %d finished file(s)", job.id, queued)
    return queued
