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

import contextlib
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
    """The upload's own name, reduced to something that cannot leave its folder.

    Not werkzeug's secure_filename: it drops every non-ASCII character, so
    'Фильм.mkv' became 'mkv' — no stem, no extension, nothing to encode — and
    'Bröderna Lejonhjärta' lost its letters before TMDb ever saw it.
    """
    from adr.utils import sanitize_filename

    name = Path(str(filename or "").replace("\\", "/")).name
    suffix = Path(name).suffix.lower()
    stem = sanitize_filename(Path(name).stem).strip(" .")
    return f"{stem or 'upload'}{suffix}"


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


def arrived(job_id: int, path: Path) -> None:
    """Record that a video file is whole, before it waits in the queue.

    A video file is its own finished rip. Recorded only when processing
    began, a restart in between left a whole file in raw/ that retry took
    for a rip killed half-way — "put the disc back in" — about an upload.
    """
    if path.suffix.lower() == ".iso":
        return
    session = get_session()
    try:
        job = session.get(Job, job_id)
        if job is not None:
            job.rip_completed_at = utcnow()
            session.commit()
    finally:
        session.close()


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
        source = f"iso:{iso}"
        # Whatever an interrupted run left half-written is not a title.
        for stale in iso.parent.glob("*.mkv"):
            with contextlib.suppress(OSError):
                stale.unlink()
        title_index = None
        if self._config.main_feature_only:
            # The same choice a disc gets. Ripping every title and encoding
            # one left the rest in raw/ for good: the cleanup keeps surplus
            # MKVs on purpose, for discs whose scan failed.
            titles = ripper.scan_disc(source, job.id)
            if titles:
                from adr.naming import main_title_index
                from adr.series import looks_like_series

                verdict = looks_like_series(titles, self._config)
                if verdict.get("is_series"):
                    # A season has no main feature; every episode is wanted.
                    job.content_type = "series"
                    job.series_season = job.series_season or 1
                    job.series_first_episode = job.series_first_episode or 1
                    session.commit()
                    log.append("rip", "The image looks like a TV season, so every "
                                      "episode is ripped. Match fixes the show name.")
                else:
                    title_index = main_title_index(titles)
                    log.append("rip", f"Ripping title {title_index + 1}, the longest of "
                                      f"{len(titles)}; the rest are left out.")
        result = ripper.rip(source, job.id, _progress_committer(job, session, "rip"),
                            title_index=title_index)
        session.refresh(job)
        if job.status == JobStatus.CANCELLED:
            return False
        if not result.success:
            fail(job.id, result.error or "MakeMKV could not read the disc image.")
            return False

        # An uploaded image has done its job, and an ISO left in raw/ is
        # several GB that nothing will ever read again. One picked off the
        # share is only linked here: the link goes, the image stays.
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


# ------------------------------------------------------------------ #
# Files already on the share
#
# Sending a film from the NAS to a laptop and back to the NAS is two network
# copies of something that never needed to move. So the files the container
# can already see can be picked where they lie: an ISO is read by MakeMKV
# straight off the share, and a video file is linked into raw/<job>/ and read
# by the encoder from there. The original is never moved, renamed or deleted.
# ------------------------------------------------------------------ #

def browse_roots(config) -> list[Path]:
    """The folders that may be browsed: the ones this app was pointed at.

    Not the whole filesystem. The page has no login, and a directory listing
    of /etc is not something anybody on the LAN needs from a disc ripper. A
    folder inside another (the film library inside the share) is covered by
    the outer one and not listed twice.
    """
    candidates = []
    for value in (config.completed_path, config.plex_path, config.tv_path,
                  getattr(config, "watch_path", "")):
        if not value:
            continue
        try:
            path = Path(value).resolve()
        except OSError:
            continue
        if path.is_dir() and path != Path("/"):
            candidates.append(path)
    roots = []
    for path in sorted(set(candidates), key=lambda p: len(p.parts)):
        if not any(path == r or r in path.parents for r in roots):
            roots.append(path)
    return roots


def resolve_inside(config, raw: str) -> Path | None:
    """*raw* as a real path, or None when it is not inside a browse root."""
    if not raw:
        return None
    try:
        path = Path(raw).resolve()
    except OSError:
        return None
    for root in browse_roots(config):
        if path == root or root in path.parents:
            return path
    return None


def list_dir(config, raw: str) -> dict:
    """One folder's sub-folders and the files in it that could be imported."""
    path = resolve_inside(config, raw)
    if path is None or not path.is_dir():
        raise ValueError("That folder is not one this app can read.")
    roots = browse_roots(config)
    dirs, files = [], []
    try:
        entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        raise ValueError(f"Could not read {path}: {exc.strerror or exc}") from exc
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir():
                dirs.append(entry.name)
            elif entry.is_file() and allowed(entry.name):
                files.append({"name": entry.name, "size": entry.stat().st_size})
        except OSError:
            continue
    parent = path.parent if path not in roots else None
    return {
        "path": str(path),
        "parent": str(parent) if parent and resolve_inside(config, str(parent)) else None,
        "dirs": dirs,
        "files": files,
        "audio_files": _audio_count(path),
    }


def _audio_count(path: Path) -> int:
    """Audio files here and one folder down — CD1/, CD2/ inside a book."""
    from adr.audiobook import AUDIO_EXTENSIONS

    count = 0
    with contextlib.suppress(OSError):
        for entry in path.iterdir():
            if entry.is_file() and entry.suffix.lower() in AUDIO_EXTENSIONS:
                count += 1
            elif entry.is_dir() and not entry.name.startswith("."):
                with contextlib.suppress(OSError):
                    count += sum(1 for f in entry.iterdir()
                                 if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS)
    return count


def import_in_place(config, raw: str, worker: "ImportWorker") -> int:
    """Start a job for a file on the share without copying it. Returns its id."""
    source = resolve_inside(config, raw)
    if source is None or not source.is_file():
        raise ValueError("That file is not one this app can read.")
    if not allowed(source.name):
        raise ValueError("Only disc images (.iso) and video files can be added.")

    job, target = create_job(config, source.name)
    # A link in raw/<job>/, for an image as much as a video: it is what a
    # restart finds to resume from, and deleting the link once the image is
    # ripped deletes nothing of the user's.
    target.symlink_to(source)
    arrived(job.id, target)
    worker.submit(job.id, target)
    logger.info("Importing %s in place as job %s", source, job.id)
    return job.id


def resume_pending(config, worker: ImportWorker) -> list[int]:
    """Hand back to the worker every upload a restart caught before it ran.

    The inbox is memory. An upload that had arrived whole and was waiting its
    turn — or was being identified, or was an ISO mid-rip — is still sitting
    in raw/<job>/, and is simply started again. One still arriving when the
    service went down is not whole, and is failed and cleaned up.
    """
    session = get_session()
    resumed = []
    try:
        waiting = (session.query(Job)
                   .filter(Job.drive == UPLOAD_DRIVE)
                   .filter(Job.status.in_([JobStatus.PENDING, JobStatus.IDENTIFYING,
                                           JobStatus.RIPPING]))
                   .all())
        for job in waiting:
            folder = Path(config.raw_path) / str(job.id)
            whole = sorted(p for p in folder.glob("*")
                           if allowed(p.name) and not p.name.endswith(".part")) \
                if folder.is_dir() else []
            for partial in folder.glob("*.part") if folder.is_dir() else []:
                with contextlib.suppress(OSError):
                    partial.unlink()
            source = next((p for p in whole if p.suffix.lower() == ".iso"), None) \
                or next(iter(whole), None)
            if source is None or not source.exists():
                job.status = JobStatus.ERROR
                job.error_message = ("The upload was still arriving when the service "
                                     "restarted, so it is not whole. Add the file again.")
                job.completed_at = utcnow()
                session.commit()
                continue
            job.status = JobStatus.PENDING
            session.commit()
            worker.submit(job.id, source)
            resumed.append(job.id)
    finally:
        session.close()
    if resumed:
        logger.info("Resumed %d upload(s) a restart interrupted", len(resumed))
    return resumed
