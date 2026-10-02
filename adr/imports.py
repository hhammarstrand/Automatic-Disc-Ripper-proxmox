"""Files added by hand: an ISO, an MP4, an MKV someone already has.

They go through the same last half as a disc. A disc is ripped into
``raw/<job id>/``, identified, then planned, encoded, named and moved into the
library from there; an upload is written into that same folder and joins the
pipeline at that point. Nothing downstream can tell the two apart, which is the
point: one place decides what a film is called and where it lives, and an
uploaded film gets exactly that.

* A video file is the raw file already. It is named from TMDb and re-encoded
  with the configured preset, like the MKVs MakeMKV writes.
* An ISO is a disc that happens to be a file. MakeMKV reads it with an
  ``iso:`` source instead of ``dev:``, and from then on it is a ripped disc.

This is not the watch folder. That encodes whatever lands in it under the name
it arrived with, and writes the result to its own output folder; an upload is
identified, and goes where a disc of the same film would go.
"""

from __future__ import annotations

import logging
import queue
import re
import shutil
import threading
from pathlib import Path

from adr.models import Job, JobStatus, get_session
from adr.utils import utcnow
from adr.watcher import VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)

#: What the job's drive column says for a file added by hand.
UPLOAD_DRIVE = "upload"

UPLOAD_EXTENSIONS = frozenset(VIDEO_EXTENSIONS)

#: Room left over beyond the file itself: an ISO is ripped next to itself
#: before it is deleted, and the encode needs space beside that.
HEADROOM_BYTES = 5 * 1024**3


def allowed(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in UPLOAD_EXTENSIONS


def safe_name(filename: str) -> str:
    """The upload's own name, reduced to something that cannot leave its folder."""
    from werkzeug.utils import secure_filename

    name = secure_filename(Path(filename or "").name)
    return name or "upload"


_YEAR = re.compile(r"(?<!\d)(19\d\d|20\d\d)(?!\d)")


def label_from_filename(filename: str) -> str:
    """What a disc label would have said, from what a file is called.

    A disc says THE_MATRIX; a file says The.Matrix.1999.1080p.BluRay.x264,
    and everything after the year is about the copy, not the film — searched
    for on TMDb it finds nothing, and the job keeps the whole string as its
    name. So the separators become spaces, bracketed tags go, and the name
    ends at the year.
    """
    stem = Path(filename or "").stem
    stem = re.sub(r"[\[(][^\])]*[\])]", lambda m: m.group(0) if _YEAR.fullmatch(m.group(0)[1:-1]) else " ", stem)
    stem = re.sub(r"[._()\[\]]+", " ", stem)
    match = _YEAR.search(stem)
    if match and match.start() > 0:
        stem = stem[:match.end()]
    return re.sub(r"\s+", " ", stem).strip() or "upload"


def create_job(config, filename: str) -> tuple[Job, Path]:
    """A job for an upload, and the path its bytes are to be written to.

    Created before a single byte arrives so the dashboard shows the upload
    while it is still coming in — an 8 GB ISO over Wi-Fi is minutes of
    nothing otherwise.
    """
    name = safe_name(filename)
    session = get_session()
    try:
        job = Job(
            disc_label=label_from_filename(name),
            drive=UPLOAD_DRIVE,
            status=JobStatus.PENDING,
            started_at=utcnow(),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        session.expunge(job)
    finally:
        session.close()
    target = Path(config.raw_path) / str(job.id) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    return job, target


def fail(job_id: int, message: str) -> None:
    session = get_session()
    try:
        job = session.get(Job, job_id)
        if job is None:
            return
        job.status = JobStatus.ERROR
        job.error_message = message
        job.completed_at = utcnow()
        session.commit()
    finally:
        session.close()
    logger.warning("Upload job %s failed: %s", job_id, message)


class ImportWorker(threading.Thread):
    """Takes finished uploads one at a time and feeds them to the encoder.

    One at a time because an ISO is a MakeMKV run, and two of those on the
    same disk at once is slower than one after the other. The encodes they
    produce still share the encoder pool with discs.
    """

    def __init__(self, config, encode_queue: queue.Queue):
        super().__init__(daemon=True, name="ImportWorker")
        self._config = config
        self._encode_queue = encode_queue
        self._inbox: queue.Queue[tuple[int, Path]] = queue.Queue()
        self._stop_event = threading.Event()

    def submit(self, job_id: int, path: Path) -> None:
        self._inbox.put((job_id, path))

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id, path = self._inbox.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.process(job_id, path)
            except Exception as exc:                    # noqa: BLE001 - reported
                logger.exception("Upload job %s failed", job_id)
                fail(job_id, f"Could not process the uploaded file: {exc}")

    # -------------------------------------------------------------- #

    def process(self, job_id: int, path: Path) -> None:
        from adr.joblog import JobLog

        log = JobLog(self._config, job_id)
        session = get_session()
        try:
            job = session.get(Job, job_id)
            if job is None or job.status == JobStatus.CANCELLED:
                return
            log.append("detect", f"Uploaded file: {path.name}")
            self._identify(job, session, log)

            if path.suffix.lower() == ".iso":
                if not self._rip_iso(job, session, path, log):
                    return
            else:
                job.rip_completed_at = utcnow()
                job.progress_rip = 1.0
                session.commit()

            from adr.retry import requeue_encode

            queued = requeue_encode(job, session, self._config, self._encode_queue)
            if not queued:
                fail(job_id, "The upload left nothing to encode.")
                return
            log.append("encode", f"{queued} file(s) queued for encoding.")
        finally:
            session.close()

    def _identify(self, job, session, log) -> None:
        """Name it the way a disc labelled with the file's name would be."""
        from adr.identify import identify_disc

        job.status = JobStatus.IDENTIFYING
        session.commit()
        try:
            info = identify_disc(job.disc_label or "", self._config.tmdb_api_key)
        except Exception:                                  # noqa: BLE001 - logged
            logger.warning("Identification failed for upload job %s", job.id, exc_info=True)
            return
        if info.high_confidence:
            job.title, job.year = info.title, info.year
            if self._config.plex_path and self._config.auto_move_to_plex:
                job.move_to_plex = True
            log.append("detect", f"Identified as {job.display_title} on TMDb.")
        else:
            log.append(
                "detect",
                f"No confident TMDb match for '{job.disc_label}'; it keeps that "
                "name. Match on the dashboard fixes it.",
            )
        job.tmdb_id, job.poster_url = info.tmdb_id, info.poster_url
        session.commit()

    def _rip_iso(self, job, session, iso: Path, log) -> bool:
        """MakeMKV the image into the folder it is sitting in, then drop it."""
        from adr.pipeline import _progress_committer, process_registry
        from adr.ripper import MakeMKVRipper

        job.status = JobStatus.RIPPING
        session.commit()
        log.append("rip", "Reading the disc image with MakeMKV.")
        ripper = MakeMKVRipper(self._config, process_registry=process_registry)
        ripper.log_sink = lambda text: log.append("rip", text)
        result = ripper.rip(f"iso:{iso}", job.id, _progress_committer(job, session, "rip"))
        session.refresh(job)
        if job.status == JobStatus.CANCELLED:
            return False
        if not result.success:
            fail(job.id, result.error or "MakeMKV could not read the disc image.")
            return False

        # The image has done its job, and an ISO left in raw/ is several GB
        # that nothing will ever read again.
        try:
            iso.unlink()
        except OSError:
            logger.warning("Could not delete %s", iso, exc_info=True)
        job.rip_completed_at = utcnow()
        job.progress_rip = 1.0
        session.commit()
        return True


def free_bytes(config) -> int:
    try:
        return shutil.disk_usage(config.raw_path).free
    except OSError:
        return 0
