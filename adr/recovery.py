"""Deal with jobs the service was in the middle of when it stopped.

A job's progress lives in the database, but the thread doing the work does
not. Stop the service — an update, a container reboot, an OOM kill — and every
job that was running is left saying RIPPING or ENCODING with nothing behind it.
Nothing ever moves them again: the dashboard shows a rip in progress for the
rest of time, and the drive it claims stays "busy" so the card offers no way to
start again.

This runs once at startup and closes them out. What happens depends on where
the job had got to, because that decides what is still on disk:

* **Mid-rip.** MakeMKV was killed; whatever it had written is a truncated MKV.
  Nothing to resume from, so the job is failed with a message saying why. The
  disc is usually still in the drive, and Rip on the dashboard starts it again.

* **Mid-encode.** The raw MKVs from the rip are intact — the expensive part is
  already done — so the encode is simply queued again. This is the case worth
  automating: the alternative is asking someone to press Retry after every
  update, for work the machine can obviously pick up by itself.

* **Between the encode and the move.** The finished files are intact but the
  destination may well be why the service was restarted at all. The job is
  failed with a message pointing at Retry, which re-checks the destination
  first — retrying into the same unmounted share would fail identically.

No notifications are sent. Restarting is a routine thing to do to this
service, and a burst of "job failed" messages every time someone updates it
would train them to be ignored.
"""

import logging

from adr import retry
from adr.models import (
    ACTIVE_STATUSES,
    RIP_PHASE_STATUSES,
    Job,
    JobStatus,
    get_session,
)
from adr.utils import utcnow

logger = logging.getLogger(__name__)

RESTART_PREFIX = "Interrupted when the service restarted."

MID_RIP_MESSAGE = (
    f"{RESTART_PREFIX} The rip was killed part-way through, so there is nothing "
    "to resume from. If the disc is still in the drive, press Rip on the "
    "dashboard to start it again."
)

MID_TRANSFER_MESSAGE = (
    f"{RESTART_PREFIX} The encoded files are intact — press Retry to move them "
    "to the destination. Retry checks the destination first, so it will say if "
    "the library is still unreachable."
)

NOTHING_LEFT_MESSAGE = (
    f"{RESTART_PREFIX} Neither the raw files nor the encoded ones are still on "
    "disk, so there is nothing to resume from."
)


def recover_interrupted_jobs(config, encode_queue) -> dict:
    """Close out every job left mid-flight. Returns what was done.

    ``{"resumed": [job ids], "failed": [job ids]}``. Never raises: a database
    that cannot be read at startup is a problem, but refusing to start the
    service over it makes a bad situation worse.
    """
    outcome: dict[str, list[int]] = {"resumed": [], "failed": []}

    try:
        session = get_session()
    except Exception:
        logger.exception("Could not open the database to recover interrupted jobs")
        return outcome

    try:
        stranded = (
            session.query(Job)
            .filter(Job.status.in_(list(ACTIVE_STATUSES)))
            .order_by(Job.id)
            .all()
        )
        if not stranded:
            return outcome

        logger.info("%d job(s) were interrupted by the last shutdown", len(stranded))
        for job in stranded:
            try:
                if _recover_one(job, session, config, encode_queue):
                    outcome["resumed"].append(job.id)
                else:
                    outcome["failed"].append(job.id)
            except Exception:
                logger.exception("Could not recover job %s", job.id)
                session.rollback()
        return outcome
    except Exception:
        logger.exception("Recovery of interrupted jobs failed")
        return outcome
    finally:
        session.close()


def _recover_one(job, session, config, encode_queue) -> bool:
    """Handle one stranded job. True if it was resumed, False if it was failed."""
    if job.status in RIP_PHASE_STATUSES:
        _fail(job, session, MID_RIP_MESSAGE)
        logger.info("Job %s was mid-rip and cannot be resumed", job.id)
        return False

    # Encode phase. The raw files are the cheapest thing to resume from and the
    # most likely to survive, so they are tried first.
    queued = retry.requeue_encode(job, session, config, encode_queue)
    if queued:
        logger.info("Job %s resumed: %d file(s) queued for encoding again", job.id, queued)
        return True

    if retry.encoded_files(job):
        _fail(job, session, MID_TRANSFER_MESSAGE)
        logger.info("Job %s has finished files waiting to be moved", job.id)
        return False

    _fail(job, session, NOTHING_LEFT_MESSAGE)
    logger.info("Job %s had nothing left on disk", job.id)
    return False


def _fail(job, session, message: str) -> None:
    job.status = JobStatus.ERROR
    job.error_message = message
    job.completed_at = utcnow()
    session.commit()
