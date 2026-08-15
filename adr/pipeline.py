"""Pipeline orchestrator for Automatic Disc Ripper.

Coordinates the full workflow per drive: detect → identify → rip → eject → encode.
Each optical drive gets its own DrivePipeline thread. Encoding jobs are dispatched
to a shared EncoderWorker pool so ripping can continue on other drives while
encoding runs.
"""

import contextlib
import json
import logging
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import requests
from sqlalchemy.exc import OperationalError as SAOperationalError

from adr import (
    disctype,
    duplicates,
    isobackup,
    joblog,
    musicbrainz,
    preflight,
    progress,
    recovery,
    seriesmode,
)
from adr.audiocd import AudioCDRipper
from adr.config import Config
from adr.disc import DiscWatcher, eject_drive
from adr.encoderfactory import build_encoder
from adr.identify import identify_disc
from adr.joblog import JobLog
from adr.models import Job, JobStatus, Track, TrackStatus, get_session, init_db
from adr.naming import (
    EXTRAS_FOLDER,
    feature_index,
    finished_files,
    only_the_feature,
    pick_main_feature,
    episode_mask,
    plan_output,
    relative_folder,
)
from adr.notify import Notifier
from adr.plex import PlexNotifier
from adr.ripper import MakeMKVRipper
from adr.series import (
    earlier_discs,
    episode_after_previous_discs,
    looks_like_series,
    parse_series_label,
)
from adr.storage import should_stage
from adr.utils import (
    BYTES_PER_MB,
    format_duration,
    kill_process_tree,
    make_plex_folder_name,
    normalize_drive,
    parse_duration,
    unique_output_dir,
    utcnow,
)
from adr.watcher import FolderWatcher

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Active process registry (for cancellation)
# ------------------------------------------------------------------ #

class ProcessRegistry:
    """Thread-safe registry of running subprocesses keyed by job ID.

    Used to kill MakeMKV / HandBrake processes when a job is cancelled.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._procs: dict[int, list[subprocess.Popen]] = {}

    def register(self, job_id: int, proc: subprocess.Popen) -> None:
        with self._lock:
            if job_id not in self._procs:
                self._procs[job_id] = []
            self._procs[job_id].append(proc)

    def unregister(self, job_id: int, proc: subprocess.Popen = None) -> None:
        with self._lock:
            if proc is None:
                self._procs.pop(job_id, None)
            else:
                procs = self._procs.get(job_id, [])
                if proc in procs:
                    procs.remove(proc)
                if not procs:
                    self._procs.pop(job_id, None)

    def kill(self, job_id: int) -> bool:
        """Kill all subprocesses for a job. Returns True if any process was killed.

        The whole tree, not the one process we hold a handle to. MakeMKV and
        HandBrake are both started in their own session so that this is
        possible, and killing only the leader leaves its children alive holding
        the stdout pipe open — which the reader loop waits on for ever, so the
        drive's lock is never released and every later attempt is told the
        drive is already ripping. The stall watchdog has always used the tree;
        cancelling used ``proc.kill()`` and did not.
        """
        with self._lock:
            procs = self._procs.pop(job_id, [])
        killed_any = False
        for proc in procs:
            if proc and proc.poll() is None:
                try:
                    kill_process_tree(proc)
                    logger.info("Killed subprocess tree for job %s (pid=%s)", job_id, proc.pid)
                    killed_any = True
                except OSError:
                    logger.warning("Could not kill subprocess for job %s", job_id, exc_info=True)
        return killed_any

    def is_cancelled(self, job_id: int) -> bool:
        """Check if a job has been cancelled in the database."""
        session = get_session()
        try:
            job = session.get(Job, job_id)
            return job is not None and job.status == JobStatus.CANCELLED
        finally:
            session.close()


# Singleton instance shared by ripper, encoder, and cancel API
process_registry = ProcessRegistry()




def _remove_superseded(task: "EncodeTask", result) -> None:
    """Delete the file an encode-again has just replaced.

    Only after the replacement exists and has bytes in it. "Encode again" from
    a finished file cannot write over its own input, so the new copy lands in a
    sibling folder — and the old one then sat in the library for ever,
    referenced by no job row, invisible to the delete preview and counted twice
    by anything reading the folder.

    Exactly the recorded path, never a walk: this runs against the user's
    library.
    """
    source = getattr(task, "supersedes", None)
    if not source:
        return
    output = getattr(result, "output_path", None)
    try:
        if not output or not Path(output).is_file() or Path(output).stat().st_size == 0:
            logger.warning(
                "Not removing %s: the replacement is missing or empty", source,
            )
            return
        if Path(source).resolve() == Path(output).resolve():
            return
        Path(source).unlink()
        logger.info("Removed the superseded copy %s", source)
        with contextlib.suppress(OSError):
            Path(source).parent.rmdir()          # only if it is now empty
    except OSError:
        logger.warning("Could not remove the superseded %s", source, exc_info=True)


def _name_the_show(job, parsed_show: str, config) -> str:
    """Give a detected series a show name worth putting on a folder.

    Returns a line for the job log, or "" when nothing changed.

    The order matters. A TMDb *TV* match is best; the name parsed off the disc
    label is second, and still better than what the job is carrying, because
    that came from the film search and is a film. Both are correctable from
    the dashboard before encoding starts.
    """
    from adr.identify import best_series

    if not parsed_show:
        return ""

    try:
        show = best_series(parsed_show, getattr(config, "tmdb_api_key", "") or "")
    except Exception:                      # noqa: BLE001 - never fail a rip
        logger.debug("TMDb TV lookup failed", exc_info=True)
        show = None

    if show:
        job.title = show["name"]
        job.year = show["year"]
        job.tmdb_id = show["tmdb_id"]
        # The poster on the job is a film's, and no longer describes this job.
        job.poster_url = show.get("poster_url")
        return (
            f"TMDb says this is '{show['name']}'"
            + (f" ({show['year']})" if show["year"] else "")
            + ". Change it from the dashboard if that is the wrong show."
        )

    if (job.title or "").strip() != parsed_show:
        job.title = parsed_show
        job.year = None
        job.tmdb_id = None
        job.poster_url = None
        return (
            f"No confident TMDb match for '{parsed_show}', so the disc label is "
            "used as the show name. The film TMDb suggested was ignored: a box "
            "set is not the film its label resembles."
        )
    return ""


def _cancelled(session, job) -> bool:
    """Whether someone has cancelled *job* since this thread last looked.

    The pipeline holds a job object for the length of a rip, and the cancel
    endpoint writes to the same row from another thread. Nothing about an ORM
    object refreshes itself, so a status this thread wrote minutes ago will
    happily overwrite a cancellation that arrived since — which is how a
    cancelled job went on to encode, transfer and announce itself as done.

    Called before each transition that would clobber it. Never raises: a
    database that cannot be read is not a reason to abandon a finished rip.
    """
    try:
        session.refresh(job)
    except Exception:                              # noqa: BLE001 - never fatal
        logger.debug("Could not re-read job %s", getattr(job, "id", "?"), exc_info=True)
        return False
    if job.status != JobStatus.CANCELLED:
        return False
    if job.completed_at is None:
        job.completed_at = utcnow()
    with contextlib.suppress(Exception):
        session.commit()
    return True


def _progress_committer(job, session, phase: str, min_interval: float = 2.0):
    """Return a progress callback that writes to the database, throttled.

    Audio extraction and disc imaging both report progress far more often than
    a dashboard can use it, and every report is a database write competing with
    the encoder workers for the same SQLite file. Once every two seconds is
    smooth to watch and cheap; the final report is always let through so a
    finished job never sits at 98%.
    """
    state = {"fraction": 0.0, "at": 0.0}

    def report(info: dict) -> None:
        fraction = min(float(info.get("overall", 0.0) or 0.0), 1.0)
        now = time.time()
        if fraction < state["fraction"]:
            return                                   # ignore backwards jumps
        if fraction < 1.0 and now - state["at"] < min_interval:
            return
        state["fraction"], state["at"] = fraction, now
        detail = {k: v for k, v in info.items() if k != "overall"}
        try:
            job.progress_rip = fraction
            job.progress_info = json.dumps({"phase": phase, **detail})
            session.commit()
        except SAOperationalError:
            with contextlib.suppress(Exception):
                session.rollback()

    return report


class _SeriesDisc(Exception):
    """Internal: the disc holds episodes, so skip main-feature selection.

    Control flow rather than an error — main-feature selection is a block of
    nested logic and this is the clearest way out of it without restructuring
    the whole method.
    """


def rename_job_output(job, session) -> None:
    """Rename output folder and finished files to Plex-style name.

    Called after encoding finishes (deferred rename) and when re-matching
    an already-completed job from the web UI.
    """
    if not job.title:
        return

    # A series folder is Show/Season NN and its files are S02E05, none of which
    # this flat rename understands. Renaming a season into a film folder is far
    # worse than leaving the name TMDb already produced.
    if (job.content_type or "movie") == "series":
        return

    new_plex_name = make_plex_folder_name(job.title, job.year)

    old_output = Path(job.output_path) if job.output_path else None
    if not old_output or not old_output.exists():
        return

    if old_output.name == new_plex_name:
        return

    new_output = old_output.parent / new_plex_name
    try:
        old_output.rename(new_output)
        job.output_path = str(new_output)

        # Both containers: a job with transcoding turned off keeps its MKVs,
        # and skipping them here would leave the old name inside a folder that
        # had just been renamed.
        video_files = finished_files(new_output)
        multi = len(video_files) > 1
        for idx, f in enumerate(video_files, start=1):
            if multi:
                part = f.stem.rsplit(" - pt", 1)[-1] if " - pt" in f.stem else str(idx)
                new_fname = f"{new_plex_name} - pt{part}{f.suffix}"
            else:
                new_fname = f"{new_plex_name}{f.suffix}"
            new_fpath = new_output / new_fname
            if f != new_fpath:
                f.rename(new_fpath)
            for t in job.tracks:
                if t.output_path and Path(t.output_path).name == f.name:
                    t.output_path = str(new_fpath)
                    break

        session.commit()
        logger.info("Renamed output for job %s to '%s'", job.id, new_plex_name)
    except OSError as exc:
        logger.warning("Rename failed for job %s: %s", job.id, exc)


def find_previous_rip(job, session):
    """An earlier successful rip of the same disc, or None.

    Matched on the disc label, which is what is known before identification has
    run. It is not a unique identifier — plenty of discs ship with labels like
    ``DVD_VIDEO`` — so this only ever annotates a job, never blocks one. A
    blank or generic label is treated as no match, since flagging every
    unlabelled disc as a duplicate of the last unlabelled disc would be worse
    than saying nothing.
    """
    label = (job.disc_label or "").strip()
    if _is_generic_label(label):
        return None
    return (
        session.query(Job)
        .filter(
            Job.id != job.id,
            Job.disc_label == job.disc_label,
            Job.status == JobStatus.DONE,
        )
        .order_by(Job.completed_at.desc())
        .first()
    )


# Labels that identify a disc format rather than a film. Matching on these
# would make every unlabelled disc a duplicate of the previous one.
_GENERIC_DISC_LABELS = frozenset({
    "DVD_VIDEO", "DVDVIDEO", "DVD", "BLURAY", "BLU-RAY", "BD_ROM", "BDROM",
    "UNTITLED", "UNKNOWN", "NO_LABEL", "LOGICAL_VOLUME_ID", "VIDEO_TS",
    "NEW_VOLUME", "MY_DISC", "MOVIE", "VIDEO", "DISC",
})

#: Words that mean the label names the *equipment*, not the film.
#:
#: A set-top DVD recorder writes its own name onto every disc it burns —
#: LG_COMBI_RECORDER, PHILIPS_DVDR, SONY_RDR — so a whole shelf of home
#: recordings shares one label. Matching on the word rather than listing every
#: brand is the only way this can cover a recorder nobody here has heard of.
_EQUIPMENT_WORDS = ("RECORDER", "COMBI", "DVDR", "RECORDING", "CAMCORDER")


def _is_generic_label(label: str) -> bool:
    """Whether *label* identifies a disc, or merely the thing that wrote it."""
    upper = label.strip().upper()
    if not upper or upper in _GENERIC_DISC_LABELS:
        return True
    return any(word in upper for word in _EQUIPMENT_WORDS)


def final_destination(job, config) -> tuple[Path, bool]:
    """Where this job's finished folder actually belongs.

    Returns ``(parent_directory, is_plex_library)``.

    A job destined for the Plex library has no business passing through
    ``completed_path`` on the way. When the library is on a NAS that detour is
    a multi-GB network write into a folder nothing reads, followed by a move —
    and if the two paths are on different mounts, a second full copy. The
    finished folder goes straight where it is going to live.
    """
    # Plex keeps films and shows in separate libraries with different naming
    # rules; a season folder in the movie library is not something Plex can
    # make sense of, so a series never goes to plex_path.
    if (job.content_type or "movie") == "series":
        if config.tv_path:
            return Path(config.tv_path), True
        return Path(config.completed_path), False

    if config.plex_path and job.move_to_plex:
        return Path(config.plex_path), True
    return Path(config.completed_path), False



def _merge_into(src: Path, dest: Path, job, log_sink=None, prefix=None) -> dict[str, str]:
    """Move the contents of *src* into an existing *dest*, clobbering nothing.

    The season merge exists because disc 2 of a box set belongs in disc 1's
    folder. What it must not do is quietly replace disc 1's episodes: two discs
    that both claim S02E01 is a numbering mistake, and overwriting one with the
    other turns a fixable mistake into a lost file. The incumbent is kept and
    the arrival is set aside beside it under a suffixed name, which is visible
    in a directory listing and undoable by renaming.

    Returns ``{relative path as it arrived: relative path it ended up at}`` for
    everything that had to be set aside. The caller needs it: the track rows
    still name the path the file *would* have had, and rebuilding them from
    that assumption pointed disc 2's rows at disc 1's episodes — after which
    deleting disc 2 "with its files" unlinked disc 1's.

    Directories are merged rather than renamed. ``Other/`` exists on every
    disc of a set, so treating it as a colliding item gave disc 2 an
    ``Other (2)/`` folder, which is not one of the names Plex recognises and
    so is not an extras folder at all any more.
    """
    prefix = prefix or Path()
    renames: dict[str, str] = {}
    dest.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.iterdir()):
        target = dest / item.name
        if item.is_dir() and target.is_dir():
            renames.update(
                _merge_into(item, target, job, log_sink, prefix / item.name),
            )
            with contextlib.suppress(OSError):
                item.rmdir()
            continue
        if target.exists():
            stem, suffix = target.stem, target.suffix
            counter = 2
            while (candidate := dest / f"{stem} ({counter}){suffix}").exists():
                counter += 1
            target = candidate
            message = (
                f"{item.name} already existed in {dest} — kept the earlier "
                f"file and saved this one as {target.name}. Two discs claiming "
                "the same episode number usually means every disc of the box "
                "set is being numbered from 1: each one is detected on its "
                "own and has no way of knowing what the last one used. "
                "Settings → Series mode names the show once and carries the "
                "episode number across discs, which is what stops this."
            )
            logger.warning("Job %s: %s", getattr(job, "id", "?"), message)
            if log_sink:
                with contextlib.suppress(Exception):
                    log_sink(message)
            renames[(prefix / item.name).as_posix()] = (
                (prefix / target.name).as_posix()
            )
        shutil.move(str(item), str(target))
    with contextlib.suppress(OSError):
        src.rmdir()          # only when it is now empty
    return renames


def transfer_to_destination(job, session, final_parent: Path) -> bool:
    """Move a finished job's folder from local staging to its real destination.

    Used when encoding was staged locally (see EncodeTask.final_dir): instead of
    HandBrake writing across the network for the whole encode, the completed
    folder is transferred once, sequentially.

    On failure the staged files are deliberately left where they are and the
    job records where to find them — losing a finished rip to a network blip
    would be far worse than an error the user can act on.

    Returns True when the files are at their destination.
    """
    src = Path(job.output_path) if job.output_path else None
    if not src or not src.exists():
        logger.warning("Transfer: staged output missing for job %s (%s)", job.id, src)
        job.error_message = f"Encoded files not found in staging ({src})."
        return False

    # A series occupies two components below the root (Show/Season NN); taking
    # only src.name would drop the show folder and scatter seasons across the
    # library root.
    relative = relative_folder(src, job)
    dest = final_parent / relative
    merging = False
    if dest.exists():
        # A film and a season collide for opposite reasons.
        #
        # Two films in one folder is one Plex entry with two movies in it, so a
        # name already taken means a different film and the folder is forked.
        # A season folder already taken means *the previous disc of this
        # season*, which is exactly where these episodes belong — forking it
        # gave a six-disc box set Season 02, Season 02 (2) … (6), with four
        # episodes in each. The filenames already carry SxxEyy, so the files
        # can be merged and any real collision is still visible per file.
        if (job.content_type or "movie") == "series":
            merging = True
        else:
            counter = 2
            while (candidate := dest.parent / f"{dest.name} ({counter})").exists():
                counter += 1
            dest = candidate

    size_mb = sum(f.stat().st_size for f in src.rglob("*") if f.is_file()) / BYTES_PER_MB
    logger.info("Transferring job %s to %s (%.0f MB)", job.id, dest, size_mb)
    started = time.monotonic()
    renames: dict[str, str] = {}
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if merging:
            # The contents, not the folder: shutil.move of a directory onto an
            # existing directory puts it *inside* it, which would give
            # Season 02/Season 02.
            renames = _merge_into(src, dest, job)
        else:
            shutil.move(str(src), str(dest))
    except (OSError, shutil.Error) as exc:
        logger.error("Transfer failed for job %s: %s", job.id, exc)
        job.error_message = (
            f"Encoding succeeded but the transfer to {dest} failed: {exc}. "
            f"The finished files are still in {src} — fix the destination and "
            "move them, or re-run the job."
        )
        return False

    elapsed = max(time.monotonic() - started, 0.001)
    logger.info(
        "Transfer complete for job %s: %.0f MB in %.0fs (%.1f MB/s)",
        job.id, size_mb, elapsed, size_mb / elapsed,
    )

    job.output_path = str(dest)
    _rebase_tracks(job, src, dest, renames)
    session.commit()
    return True


def _rebase_tracks(job, src: Path, dest: Path, renames: dict | None = None) -> None:
    """Point each track at where its file now is.

    Rebuilding the path as ``dest / Path(old).name`` drops any component
    between the job folder and the file — which is exactly where extras live,
    ``Other/Extra 1.mp4``. The row then named a path that did not exist, so the
    delete preview could not see the extras and the Play button pointed at
    nothing. The part below the job folder is preserved instead.

    *renames* is what the merge had to set aside. Without it this computed the
    name the file would have had if nothing had collided — which is the name
    the *previous* disc's file already has. Disc 2's rows then named disc 1's
    episodes, so the Play button opened the wrong file and deleting disc 2
    with its files unlinked disc 1's, leaving disc 2's own "(2)" copy
    referenced by nothing and invisible to every later preview.
    """
    for t in job.tracks:
        if not t.output_path:
            continue
        old = Path(t.output_path)
        try:
            relative = old.relative_to(src).as_posix()
            t.output_path = str(dest / (renames or {}).get(relative, relative))
        except ValueError:
            # Not below the folder we moved — an older row, or a path from
            # before staging existed. Its own name is the best guess left.
            t.output_path = str(dest / old.name)


def move_to_plex(job, session, config) -> bool:
    """Move a finished job's output folder to the Plex library.

    Returns True if the move succeeded, False otherwise.
    """
    library = config.tv_path if (job.content_type or "movie") == "series" else config.plex_path
    if not library:
        return False
    if not job.move_to_plex:
        return False
    if not job.output_path:
        return False

    src = Path(job.output_path)
    if not src.exists():
        logger.warning("Plex move: source does not exist for job %s: %s", job.id, src)
        return False

    # Normally the transfer already delivered the folder here — see
    # final_destination(). Nothing to move, just record where it is.
    if src.parent == Path(library) or str(src).startswith(str(Path(library)) + "/"):
        if job.plex_path != str(src):
            job.plex_path = str(src)
            session.commit()
        return True

    dest = Path(library) / relative_folder(src, job)

    # Same rule as the transfer: a film's folder is forked on collision, a
    # season's is merged into. See transfer_to_destination.
    merging = False
    if dest.exists():
        if (job.content_type or "movie") == "series":
            merging = True
        else:
            counter = 2
            while True:
                candidate = dest.parent / f"{dest.name} ({counter})"
                if not candidate.exists():
                    dest = candidate
                    break
                counter += 1

    renames: dict[str, str] = {}
    try:
        if merging:
            renames = _merge_into(src, dest, job)
        else:
            shutil.move(str(src), str(dest))
        job.plex_path = str(dest)
        job.output_path = str(dest)
        _rebase_tracks(job, src, dest, renames)
        session.commit()
        logger.info("Moved job %s to Plex: %s", job.id, dest)
        return True
    except (OSError, shutil.Error) as exc:
        logger.error("Plex move failed for job %s: %s", job.id, exc)
        return False


# ------------------------------------------------------------------ #
# Encode queue item
# ------------------------------------------------------------------ #

class EncodeTask:
    """A single file to encode, dispatched to the encoder worker pool.

    ``output_dir`` is where HandBrake writes. When ``final_dir`` is set it is
    a *staging* directory on local disk, and the finished folder is transferred
    to ``final_dir`` once every track of the job has encoded — one sequential
    copy instead of HandBrake writing across the network for the whole encode.
    """

    def __init__(
        self,
        job_id: int,
        track_id: int,
        input_path: Path,
        output_dir: Path,
        output_filename: str,
        final_dir: Path | None = None,
        passthrough: bool = False,
        supersedes: Path | None = None,
    ):
        self.job_id = job_id
        self.track_id = track_id
        self.input_path = input_path
        self.output_dir = output_dir
        #: The file this encode replaces, deleted once the new one is written.
        #: Set only by "encode again" reading a finished file: the encoder
        #: cannot write over what it is reading, so the result lands in a
        #: sibling folder — and without this the old copy stayed in the
        #: library for ever, referenced by no job row and therefore invisible
        #: to the delete preview.
        self.supersedes = supersedes
        self.output_filename = output_filename
        self.final_dir = final_dir
        #: Keep the MKV as it came off the disc instead of transcoding it.
        #: The task still goes through the worker pool, so everything after
        #: the encode — renaming, the transfer, the Plex move, cleanup and
        #: notifications — happens exactly as it does for an encoded job.
        self.passthrough = passthrough


# ------------------------------------------------------------------ #
# Encoder worker (shared across all drives)
# ------------------------------------------------------------------ #

class EncoderWorker(threading.Thread):
    """Consumes EncodeTask items from a queue and transcodes them."""

    def __init__(self, config: Config, task_queue: queue.Queue, name: str = "EncoderWorker"):
        super().__init__(daemon=True, name=name)
        self._config = config
        self._queue = task_queue
        self._encoder = build_encoder(config)
        self._encoder._backend = config.encoder_backend
        self._encoder._process_registry = process_registry
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _current_encoder(self):
        """The encoder the *current* settings ask for.

        Built once and kept, but re-built when the backend setting changes
        underneath us. Switching from HandBrake to the GPU otherwise did
        nothing until the service was restarted — a setting that appears to
        take effect and does not is worse than one that refuses to change,
        and this application has spent long enough on exactly that failure.
        """
        wanted = self._config.encoder_backend
        if getattr(self._encoder, "_backend", None) != wanted:
            logger.info("Encoder backend changed to %r; rebuilding", wanted)
            self._encoder = build_encoder(self._config)
            self._encoder._backend = wanted
            self._encoder._process_registry = process_registry
        return self._encoder

    def run(self) -> None:
        logger.info("%s started", self.name)
        while not self._stop_event.is_set():
            try:
                task: EncodeTask = self._queue.get(timeout=2)
            except queue.Empty:
                continue

            # A worker that dies takes every future encode with it, silently:
            # tasks keep arriving on the queue and nothing ever picks them up.
            # _process_task handles its own errors, so reaching here means
            # something outside it went wrong — and one bad task is not a
            # reason to stop encoding for the life of the service.
            try:
                self._process_task(task)
            except Exception:
                logger.exception(
                    "%s could not process job %s; carrying on", self.name, task.job_id,
                )
            finally:
                self._queue.task_done()
        logger.info("%s stopped", self.name)

    def _process_task(self, task: EncodeTask) -> None:
        session = None
        try:
            session = get_session()
            job = session.get(Job, task.job_id)
            track = session.get(Track, task.track_id)
            if not job or not track:
                logger.error("Encode task references missing job/track: job=%s track=%s", task.job_id, task.track_id)
                return

            # Refresh to catch cancellations that happened while queued
            session.refresh(job)
            if job.status == JobStatus.CANCELLED:
                logger.info("Skipping encode for cancelled job %s", task.job_id)
                return

            track.status = TrackStatus.ENCODING
            job.status = JobStatus.ENCODING
            if not job.encode_started_at:
                job.encode_started_at = utcnow()
            session.commit()

            # Query DB for accurate counts (other workers may have updated)
            total_tracks = session.query(Track).filter(Track.job_id == task.job_id).count()

            _last_enc_commit = [0.0]
            _fps_samples = []  # collect fps readings for avg

            def on_progress(info: dict) -> None:
                """Update per-track and overall encode progress."""
                now = time.time()
                if now - _last_enc_commit[0] < 2.0:
                    return

                try:
                    raw_progress = float(info.get("progress", 0.0) or 0.0)
                    raw_progress = min(max(raw_progress, 0.0), 1.0)
                    state = str(info.get("state", "working") or "working").lower()
                    pass_num = int(info.get("pass_num", 0) or 0)
                    pass_total = max(int(info.get("pass_total", 1) or 1), 1)

                    # HandBrake reports Working.Progress per pass, not for the
                    # whole encode. Spread multi-pass work across the full 0-99%
                    # range and reserve the last 1% for muxing/finalization.
                    if state == "working":
                        current_pass = min(max(pass_num, 1), pass_total)
                        p = (((current_pass - 1) + raw_progress) / pass_total) * 0.99
                    elif state == "muxing":
                        p = 0.99 + (raw_progress * 0.01)
                    elif state == "scanning":
                        p = raw_progress * 0.01
                    else:
                        p = raw_progress

                    # Refresh done count to account for sibling threads
                    done_count = session.query(Track).filter(
                        Track.job_id == task.job_id, Track.status == TrackStatus.DONE
                    ).count()

                    overall = (done_count + p) / total_tracks if total_tracks > 0 else p
                    overall = min(overall, 1.0)

                    current_overall = job.progress_encode or 0.0
                    if overall < current_overall:
                        return  # ignore backwards jumps from concurrent tracks

                    job.progress_encode = overall

                    # Build rich progress info, safely handling None values from API
                    fps = info.get("fps")
                    fps = fps if fps is not None else 0.0
                    fps_avg = info.get("fps_avg")
                    fps_avg = fps_avg if fps_avg is not None else 0.0
                    eta = info.get("eta_seconds")
                    eta = eta if eta is not None else 0
                    pass_num = info.get("pass_num")
                    pass_num = pass_num if pass_num is not None else 0
                    pass_total = info.get("pass_total")
                    pass_total = pass_total if pass_total is not None else 1

                    # Collect fps samples for average
                    if fps > 0:
                        _fps_samples.append(fps)
                        # Update avg_fps incrementally so it's visible during encoding
                        job.avg_fps = sum(_fps_samples) / len(_fps_samples)

                    pi = {
                        "phase": "encoding",
                        "track_current": done_count + 1,
                        "track_total": total_tracks,
                        "track_progress": round(raw_progress, 4),
                        "effective_progress": round(p, 4),
                        "eta_seconds": eta,
                        "fps": round(fps, 1),
                        "fps_avg": round(fps_avg, 1),
                        "pass_num": pass_num,
                        "pass_total": pass_total,
                        "state": info.get("state", "working"),
                    }
                    job.progress_info = json.dumps(pi)

                    committed = False
                    for attempt in range(3):
                        try:
                            session.commit()
                            committed = True
                            break
                        except SAOperationalError:
                            try:
                                session.rollback()
                            except Exception:
                                logger.debug("Rollback failed during encode progress retry", exc_info=True)
                            if attempt < 2:
                                time.sleep(0.1 * (attempt + 1))
                    if committed:
                        _last_enc_commit[0] = now
                    else:
                        logger.warning("Encode progress commit failed after 3 attempts for job %s", task.job_id)
                except Exception:
                    logger.warning("Encode progress update failed for job %s", task.job_id, exc_info=True)
                    try:
                        session.rollback()
                    except Exception:
                        logger.debug("Rollback failed during encode progress recovery", exc_info=True)

            # Capture HandBrake's own output against this job, so a preset or
            # codec failure is readable from the UI rather than journalctl.
            job_log = JobLog(self._config, task.job_id)
            if task.passthrough:
                job_log.append(
                    "encode",
                    f"Transcoding is off — keeping {task.input_path.name} as "
                    f"{task.output_filename}.mkv",
                )
                result = self._passthrough(task)
                job_log.append(
                    "encode",
                    "File kept without transcoding."
                    if result.success else f"Could not keep the file: {result.error}",
                )
            else:
                job_log.append(
                    "encode", f"Encoding {task.input_path.name} -> {task.output_filename}",
                )
                encoder = self._current_encoder()
                encoder.log_sink = job_log.sink("encode")
                try:
                    result = encoder.encode(
                        input_path=task.input_path,
                        output_dir=task.output_dir,
                        output_filename=task.output_filename,
                        progress_callback=on_progress,
                        job_id=task.job_id,
                    )
                finally:
                    # Workers are shared between jobs; a stale sink would write
                    # one job's output into another job's log.
                    encoder.log_sink = None
                job_log.append(
                    "encode",
                    "Encode finished." if result.success else f"Encode failed: {result.error}",
                )

            # Check if cancelled during encode
            session.refresh(job)
            if job.status == JobStatus.CANCELLED:
                logger.info("Job %s cancelled during encode", task.job_id)
                # Say so on the track, and take the half-written file away.
                #
                # HandBrake writes straight to the final name with no temp
                # file, so what is on disk now is a truncated MP4 that a
                # directory listing cannot tell from a finished one. Returning
                # here without touching either left the track saying ENCODING
                # for ever and the stub in place — and Retry then offered to
                # move it into the library as an intact encode.
                track.status = TrackStatus.ERROR
                track.error_message = "Cancelled during encoding."

                # Only a genuinely partial *encode*, and never a passthrough.
                #
                # With transcoding off, _passthrough MOVES the ripped MKV into
                # place — the file at output_path is the complete rip and the
                # source no longer exists, so unlinking it destroys the only
                # copy. A successful result is finished by definition too,
                # whichever path produced it: the cancel simply arrived after
                # the encoder had already written the file.
                partial = getattr(result, "output_path", None)
                if partial and not task.passthrough and not result.success:
                    try:
                        Path(partial).unlink()
                        logger.info("Removed the partial encode %s", partial)
                    except OSError:
                        logger.debug("Could not remove %s", partial, exc_info=True)
                elif partial and result.success:
                    # It finished. Say so on the track rather than calling a
                    # complete file an error, so a retry can still resume the
                    # transfer instead of encoding it all over again.
                    track.status = TrackStatus.DONE
                    track.output_path = str(partial)
                    track.error_message = None
                    logger.info(
                        "Cancelled after track %s had already finished; keeping %s",
                        track.track_number, partial,
                    )
                job.completed_at = utcnow()
                session.commit()
                return

            if result.success:
                track.status = TrackStatus.DONE
                track.output_path = str(result.output_path)
                logger.info("Track %s encoded: %s", track.track_number, result.output_path)
                _remove_superseded(task, result)
            else:
                track.status = TrackStatus.ERROR
                track.error_message = result.error
                logger.error("Track %s encode failed: %s", track.track_number, result.error)

            # Check if all tracks for this job are done
            session.refresh(job)
            all_done = all(t.status == TrackStatus.DONE for t in job.tracks)
            any_error = any(t.status == TrackStatus.ERROR for t in job.tracks)

            if all_done:
                job.status = JobStatus.DONE
                job.progress_encode = 1.0
                job.completed_at = utcnow()
                job.output_path = str(task.output_dir)
                if _fps_samples:
                    job.avg_fps = sum(_fps_samples) / len(_fps_samples)
                logger.info("Job %s complete: %s", job.id, job.display_title)

                # If the user re-matched via TMDb while ripping/encoding,
                # the title may have changed.  Rename the output folder
                # and files to the new Plex-style name.  Doing this before the
                # transfer keeps it a cheap local rename.
                rename_job_output(job, session)

                # Encoded to local staging — now do the single transfer to the
                # real destination (typically the NAS). The destination is
                # resolved again here rather than trusting task.final_dir,
                # because the user can toggle the Plex flag while the encode
                # runs and this is the last moment it can still be honoured for
                # free.
                if task.final_dir is not None:
                    dest_parent, _ = final_destination(job, self._config)
                    if not transfer_to_destination(job, session, dest_parent):
                        # The files are intact in staging; say so rather than
                        # reporting a success the user does not have.
                        job.status = JobStatus.ERROR
                        job.completed_at = utcnow()
                        session.commit()
                        Notifier(self._config).job_failed(job)
                        return

                # Usually a no-op by now: the transfer above already put the
                # folder in the library. This still catches the un-staged case
                # and a flag toggled after the transfer — both local moves.
                move_to_plex(job, session, self._config)

                # Clean up raw MKV files / watch folder source
                self._cleanup_raw(job.id, session)
                self._cleanup_watch_source(job, task)

                # The disc is done. Tell whoever walked away, and tell Plex so
                # the film is visible now rather than after the next scheduled
                # scan. Both are best-effort: the film is on disk either way.
                session.commit()

                # Is that all of them? Feeding a box set one disc at a time,
                # that is the question after every single disc, and nothing
                # answered it — whether episode 9 existed anywhere was
                # something to notice weeks later in Plex.
                season = {}
                if (job.content_type or "movie") == "series":
                    from adr import seasoncheck

                    season = seasoncheck.check(job, self._config)
                    if season["text"]:
                        job_log.append("done", season["text"])

                Notifier(self._config).job_done(
                    job, job.output_path or "", season.get("text", ""),
                )
                PlexNotifier(self._config).refresh_for(job.output_path or "")
            elif any_error and all(t.status in (TrackStatus.DONE, TrackStatus.ERROR) for t in job.tracks):
                job.status = JobStatus.ERROR
                # Name the reason, not the symptom. "One or more tracks failed
                # to encode" is true of every encode failure there has ever
                # been and sends the reader to the log to find out which and
                # why — which is exactly what this line is for.
                failed = [t for t in job.tracks if t.status == TrackStatus.ERROR]
                reasons = {t.error_message for t in failed if t.error_message}
                if len(failed) == 1 and reasons:
                    job.error_message = f"Encoding failed: {reasons.pop()}"
                elif reasons:
                    job.error_message = (
                        f"{len(failed)} of {len(job.tracks)} tracks failed to encode. "
                        + " | ".join(sorted(reasons))
                    )
                else:
                    job.error_message = (
                        f"{len(failed)} of {len(job.tracks)} tracks failed to encode, "
                        "and HandBrake gave no reason. See the tool output."
                    )
                job.completed_at = utcnow()
                session.commit()
                Notifier(self._config).job_failed(job)

            session.commit()
        except Exception:
            logger.exception("Error processing encode task for job %s", task.job_id)
            if session is not None:
                with contextlib.suppress(Exception):
                    session.rollback()
        finally:
            if session is not None:
                with contextlib.suppress(Exception):
                    session.close()

    @staticmethod
    def _passthrough(task: "EncodeTask"):
        """Move the ripped MKV into place instead of transcoding it.

        A move rather than a copy: the raw file and the finished file are the
        same bytes, and copying would need twice the disk for no benefit. The
        cleanup that follows removes an empty raw directory either way.

        Falls back to a copy when the two are on different filesystems, which
        is what os.rename refuses to do — raw_path and staging_path are both
        local by design, but nothing stops someone pointing one elsewhere.
        """
        from adr.encoder import EncodeResult

        result = EncodeResult()
        result.input_path = task.input_path
        destination = task.output_dir / f"{task.output_filename}.mkv"
        try:
            # The destination's parent, not the output directory. An extra's
            # filename is 'Other/Extra 1', so the two differ by exactly the
            # subfolder Plex reads extras out of — and without it the move
            # failed for every extra on the disc, which failed the whole job.
            # HandBrake's own path has created it since extras existed; this
            # one never did, so turning transcoding off broke them.
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(task.input_path), str(destination))
        except (OSError, shutil.Error) as exc:
            result.error = f"Could not move {task.input_path.name} into place: {exc}"
            logger.error("Passthrough failed for job %s: %s", task.job_id, exc)
            return result
        result.success = True
        result.output_path = destination
        logger.info("Job %s: kept %s without transcoding", task.job_id, destination.name)
        return result

    def _cleanup_raw(self, job_id: int, session=None) -> None:
        """Remove the temporary raw MKV directory for a completed job.

        Unless titles were deliberately kept there. When the pre-rip scan
        cannot run, "main feature only" rips the whole disc and encodes just
        the film, and the job log promises the rest are still in raw/ — so
        deleting them the moment the one encode finished made that sentence
        false, and the fallback it describes impossible.

        The evidence is arithmetic: more MKVs on disk than the job has tracks
        means files were dropped on purpose. No schema change, and it cannot
        drift out of step with the decision that produced it.
        """
        raw_dir = self._config.raw_path / str(job_id)
        if not raw_dir.exists():
            return

        try:
            on_disk = sorted(p for p in raw_dir.glob("*.mkv") if p.is_file())
        except OSError:
            on_disk = []
        tracks = 0
        job_tracks = []
        if session is not None:
            with contextlib.suppress(Exception):
                job_tracks = list(
                    session.query(Track).filter(Track.job_id == job_id),
                )
                tracks = len(job_tracks)

        # Count what was *ripped*, not what survives.
        #
        # With transcoding off, _passthrough moves each encoded track's MKV out
        # of raw/ — so the files left behind are exactly the dropped ones and
        # the comparison against the track count came out equal, deleting the
        # very titles the job log promised. Adding back the tracks whose file
        # has already left restores the original count.
        #
        # The track's *source* filename is what decides that, and getting this
        # wrong is expensive. It used to ask whether the track's OUTPUT name
        # was in raw/ — which for an ordinary transcoded job is "Film
        # (2020).mp4", never in raw/ by definition. So every track counted as
        # moved out, ripped came to twice the track count, the "titles were
        # deliberately kept" branch fired on every disc, and nothing was ever
        # deleted: each disc left its whole rip on the container disk for
        # ever, 20-40 GB at a time, on the same disk the database lives on.
        moved_out = sum(
            1 for t in (job_tracks or [])
            if t and getattr(t, "filename", None)
            and not (raw_dir / str(t.filename)).exists()
        ) if job_tracks else 0
        ripped = len(on_disk) + moved_out

        if on_disk and tracks and ripped > tracks:
            kept = ripped - tracks
            megabytes = sum(f.stat().st_size for f in on_disk) / BYTES_PER_MB
            logger.info(
                "Keeping %s: %d title(s) were ripped but not encoded (%.0f MB)",
                raw_dir, kept, megabytes,
            )
            with contextlib.suppress(Exception):
                JobLog(self._config, job_id).append(
                    "encode",
                    f"{kept} ripped title(s) that were not encoded are still in "
                    f"{raw_dir} ({megabytes:.0f} MB). Delete the job with its "
                    "files from the History page to reclaim the space.",
                )
            return

        try:
            shutil.rmtree(raw_dir)
            logger.info("Cleaned up raw directory: %s", raw_dir)
        except OSError:
            logger.warning("Could not clean up %s", raw_dir, exc_info=True)

    @staticmethod
    def _cleanup_watch_source(job: Job, task: "EncodeTask") -> None:
        """Remove source file for a completed watch-folder job."""
        if job.drive != "watch":
            return
        try:
            if task.input_path.exists():
                task.input_path.unlink()
                logger.info("Deleted watch folder source: %s", task.input_path)
        except OSError:
            logger.warning("Could not delete watch source %s", task.input_path, exc_info=True)


# ------------------------------------------------------------------ #
# Drive pipeline (one per optical drive)
# ------------------------------------------------------------------ #

class DrivePipeline:
    """Manages the full rip→eject→encode workflow for a single optical drive.

    Disc insertion events are received from the DiscWatcher callback and
    processed sequentially per drive.
    """

    def __init__(
        self,
        drive_letter: str,
        config: Config,
        encode_queue: queue.Queue,
    ):
        self.drive = drive_letter
        self._config = config
        self._encode_queue = encode_queue
        self._ripper = MakeMKVRipper(config, process_registry=process_registry)
        self._lock = threading.Lock()  # Prevent concurrent rips on same drive

    @property
    def is_busy(self) -> bool:
        """Whether a rip is running on this drive right now."""
        return self._lock.locked()

    def handle_disc_inserted(self, drive: str, volume_name: str | None,
                             manual: bool = False) -> None:
        """Callback invoked by DiscWatcher when a disc is inserted.

        Only processes if the event is for our drive. Runs the full pipeline
        in a new thread so the watcher isn't blocked.

        *manual* means someone pressed a button rather than a disc appearing.
        The pipeline is identical; only the "disc inserted" notification is
        skipped, since whoever asked for it is already standing there.
        """
        if normalize_drive(drive) != normalize_drive(self.drive):
            return
        # Check if drive was disabled at runtime via settings UI
        if normalize_drive(drive) in self._config.disabled_drives:
            logger.info("Drive %s is disabled — ignoring disc event", drive)
            return
        # Confirm there is actually something in the drive before a job exists.
        #
        # The watcher used to say "disc present" for an empty tray — a
        # non-blocking open of an optical drive succeeds either way — so the
        # service came up, started a job on an empty drive, and failed it with
        # a MakeMKV exit code. A red job for a drive nobody had put a disc in
        # is worse than no job: it needs clearing, and it says nothing.
        from adr.disc import NOTHING_TO_RIP, media_status

        # No display name here on purpose: this detail is only ever logged.
        state = media_status(drive)
        if state["state"] in NOTHING_TO_RIP:
            logger.info("Ignoring disc event for %s: %s", drive, state["detail"])
            return
        if not manual:
            Notifier(self._config).disc_inserted(drive, volume_name)
        thread = threading.Thread(
            target=self._run_pipeline,
            args=(volume_name,),
            daemon=True,
            name=f"Pipeline-{self.drive}",
        )
        thread.start()

    def _run_pipeline(self, volume_name: str | None) -> None:
        """Execute the full pipeline for one disc."""
        if not self._lock.acquire(blocking=False):
            logger.warning("Drive %s is already ripping — ignoring new disc event", self.drive)
            return

        # Opening the session is inside the try because it can fail — a locked
        # or corrupt database — and outside it the exception would escape past
        # the finally that releases the lock. The drive would then be busy for
        # the life of the service, with no disc in it and nothing running.
        session = None
        job: Job | None = None
        try:
            session = get_session()

            # 1. Create job
            job = Job(
                disc_label=volume_name,
                drive=self.drive,
                status=JobStatus.IDENTIFYING,
                started_at=utcnow(),
            )
            session.add(job)
            session.commit()
            logger.info("Job %s created for drive %s: label=%s", job.id, self.drive, volume_name)

            # 1a. What is actually in the drive?
            #
            # Everything below this point assumes a disc with video titles on
            # it. An audio CD or a data disc has none, and handing one to
            # MakeMKV produces a failure indistinguishable from a drive that
            # cannot be reached — which is the worst kind, because it sends
            # someone off debugging hardware that is fine.
            disc = disctype.classify(self.drive)
            # Recorded for every disc, not only the unusual ones. A video disc
            # that said nothing here left no way to tell "classification ran
            # and chose video" from "classification never ran" — and that is
            # the first question to ask when a disc that used to rip stops.
            JobLog(self._config, job.id).append("detect", disc.detail)
            logger.info("Job %s: %s", job.id, disc.detail)
            if disc.kind != disctype.KIND_VIDEO:
                job.content_type = disc.kind
                session.commit()
            if disc.kind == disctype.KIND_AUDIO:
                self._run_audio_cd(job, session, disc)
                return
            if disc.kind == disctype.KIND_DATA:
                self._run_data_disc(job, session, disc)
                return

            # Series mode overrides everything about what this disc is: the
            # user has said so explicitly, which beats a guess from durations.
            if seriesmode.apply_to(job, self._config):
                session.commit()
                logger.info("Job %s: %s", job.id, seriesmode.describe(self._config))
                JobLog(self._config, job.id).append(
                    "detect", seriesmode.describe(self._config),
                )


            # 1b. Fail fast if the finished files have nowhere to go. A rip
            # takes tens of minutes and several GB; discovering at the end that
            # the NAS was never mounted wastes the entire run.
            #
            # The check lives in adr.preflight so the dashboard can run the
            # same one and warn *before* a disc goes in. A warning that
            # disagreed with this gate would be worse than none — it would
            # either promise a rip that then fails, or complain about one that
            # would have worked.
            dest_err = preflight.destination_blocker(self._config)
            if dest_err:
                job.status = JobStatus.ERROR
                job.error_message = dest_err
                job.completed_at = utcnow()
                session.commit()
                logger.error("Job %s aborted before ripping: %s", job.id, dest_err)
                # Into the job log too. Failing here wrote nothing to it, so
                # the one place someone looks for "why did this fail" — the
                # terminal icon in the history — was empty for exactly the
                # failure that happens before any tool has run.
                JobLog(self._config, job.id).append(
                    "detect",
                    f"Aborted before ripping: {dest_err}",
                )
                Notifier(self._config).job_failed(job)
                return

            # 2. Identify disc via TMDb.
            #
            # Skipped entirely in series mode: it is a *film* search, and for a
            # box-set disc it returns a confident-looking film that would
            # overwrite the show the user just named. The whole point of the
            # mode is that they have already answered this question.
            tmdb_confident = False
            if seriesmode.is_active(self._config):
                logger.info(
                    "Job %s: skipping film identification, series mode names this disc",
                    job.id,
                )
            else:
                try:
                    info = identify_disc(volume_name or "", self._config.tmdb_api_key)
                    tmdb_confident = info.high_confidence
                    if tmdb_confident:
                        job.title = info.title
                        job.year = info.year
                        logger.info("TMDb high-confidence match: %s (conf=%.2f)", job.display_title, info.confidence)
                    else:
                        # Keep disc label as title, but still store metadata for UI display
                        logger.info(
                            "TMDb low confidence (%.2f < %.2f) — keeping disc label '%s' instead of '%s'",
                            info.confidence, 0.85, volume_name, info.title
                        )
                    # Always store TMDb metadata (poster etc.) for the web UI
                    job.tmdb_id = info.tmdb_id
                    job.poster_url = info.poster_url

                    # Auto-flag for Plex move if confident match and feature enabled
                    if tmdb_confident and self._config.plex_path and self._config.auto_move_to_plex:
                        job.move_to_plex = True
                        logger.info("Job %s flagged for Plex move", job.id)

                    session.commit()
                except (requests.RequestException, ValueError, KeyError):
                    logger.warning("Identification failed for job %s, continuing with label", job.id, exc_info=True)

            # 2b. Has this film been ripped before?
            #
            # After identification, not before: until TMDb has run, the only
            # thing known about the disc is its label, which is the weakest of
            # the three signals duplicates.py can use. Before the rip, because
            # the whole point is not to spend forty minutes on a file that is
            # already in the library.
            duplicate = duplicates.find_duplicate(job, session, self._config)
            if duplicate:
                job.duplicate_of = duplicate["job_id"]
                session.commit()
                logger.warning("Job %s: %s", job.id, duplicate["detail"])
                JobLog(self._config, job.id).append("detect", duplicate["detail"])
                Notifier(self._config).duplicate(job, duplicate["detail"])

                if self._config.skip_duplicates and duplicates.blocks_a_rip(duplicate):
                    job.status = JobStatus.CANCELLED
                    job.error_message = (
                        f"Skipped as a duplicate. {duplicate['detail']} "
                        "Turn off 'Skip discs already ripped' under Settings to "
                        "rip it anyway."
                    )
                    job.completed_at = utcnow()
                    session.commit()
                    logger.info("Job %s skipped as a duplicate", job.id)
                    if self._config.should_eject(self.drive):
                        eject_drive(self.drive)
                    return

            # 3. Prepare rip
            # Smart main-feature selection: scan disc first, pick longest title
            #
            # Every branch below writes to the job's own log, not only to the
            # service log. "Main feature only was on and the disc came back
            # with sixteen titles" is a question the job log has to be able to
            # answer on its own, because it is the log the person who put the
            # disc in can actually read.
            job_log = JobLog(self._config, job.id)
            selected_title_index = None
            if self._config.main_feature_only:
                try:
                    logger.info("Main feature mode: scanning disc to find longest title...")
                    job_log.append(
                        "rip", "Main feature only: scanning the disc to find its longest title.",
                    )
                    # MakeMKV's own words about the scan, in the job log. When
                    # the scan finds nothing this is the only place that says
                    # why, and "why" decides whether the whole disc gets ripped.
                    self._ripper.log_sink = JobLog(self._config, job.id).sink("rip")
                    try:
                        scan_titles = self._ripper.scan_disc(self.drive, job.id)
                    finally:
                        self._ripper.log_sink = None

                    # A scan that was stopped reports no titles, which is
                    # indistinguishable from a disc that has none — and the
                    # answer to "no titles" is to rip all of them. So a Cancel
                    # pressed during the scan started a full rip of the disc
                    # the user had just cancelled.
                    if self._ripper.scan_cancelled:
                        # A signal killed the scan, and the only two things
                        # that send one are Cancel and the service stopping.
                        # The database says which: Cancel writes CANCELLED
                        # before it kills anything, a shutdown writes nothing.
                        # Claiming a restart was a cancellation would put the
                        # wrong sentence in the job log and hide the restart
                        # from the recovery pass on the way back up.
                        session.refresh(job)
                        if job.status == JobStatus.CANCELLED:
                            job.completed_at = utcnow()
                            session.commit()
                            job_log.append("rip", "Cancelled while scanning the disc.")
                            logger.info("Job %s cancelled during the disc scan", job.id)
                        else:
                            job_log.append(
                                "rip",
                                "The disc scan was stopped before it finished, so "
                                "nothing was ripped. Press Rip to start again.",
                            )
                            logger.info(
                                "Job %s: the disc scan was stopped; leaving the job "
                                "for the recovery pass", job.id,
                            )
                        return
                    logger.info("Scan found %d title(s): %s",
                                len(scan_titles),
                                {idx: (t.get("duration", "?"), t.get("size", "?"))
                                 for idx, t in scan_titles.items()})
                    # Before picking a "main feature", ask whether the disc
                    # even has one. Six titles of 42 minutes is a box set, and
                    # ripping only the longest would silently discard five
                    # episodes. Detection only annotates — the user confirms,
                    # because calling a film a series renames it into a season
                    # folder and that is annoying to undo.
                    # In series mode the answer is already given; the
                    # heuristic only decides for discs nobody has spoken for.
                    if (job.content_type or "movie") == "series":
                        selected_title_index = None
                        raise _SeriesDisc

                    verdict = looks_like_series(scan_titles, self._config)
                    if verdict["is_series"] and self._config.series_detection:
                        job.content_type = "series"
                        guess = parse_series_label(job.disc_label or "")
                        job.series_season = guess["season"] or 1
                        # Which disc of the box set this is, when the label
                        # says so, and where that leaves the numbering. Only
                        # the label may start this — see
                        # episode_after_previous_discs for why the season
                        # folder alone cannot.
                        first, why = episode_after_previous_discs(
                            guess["disc"],
                            earlier_discs(
                                session, guess["show"], job, job.series_season,
                            ),
                        )
                        job.series_first_episode = first

                        # And *which* show. Identification ran TMDb's film
                        # search, which for a box set returns a
                        # confident-looking film — so a detected series was
                        # named after whatever movie its label resembled, and
                        # the only cure was to open the dialog and search by
                        # hand. The label already parses to a show name; this
                        # is the dialog's own search, run without being asked.
                        named = _name_the_show(job, guess["show"], self._config)
                        if named:
                            job_log.append("detect", named)
                        session.commit()
                        logger.info(
                            "Job %s looks like a TV disc: %s", job.id, verdict["reason"],
                        )
                        job_log.append("detect", verdict["reason"])
                        job_log.append(
                            "detect",
                            f"Assuming season {job.series_season} starting at "
                            f"episode {job.series_first_episode}. "
                            "Change it in the web UI before encoding starts.",
                        )
                        if why:
                            job_log.append("detect", why)
                        # Every episode is wanted, not just the longest.
                        selected_title_index = None
                        raise _SeriesDisc

                    if scan_titles:
                        # Parse durations and pick longest; break ties by size_bytes then lowest index
                        def _sort_key(item):
                            idx, info = item
                            dur = parse_duration(info.get("duration", "0:00:00"))
                            try:
                                size = int(info.get("size_bytes", 0))
                            except (ValueError, TypeError):
                                size = 0
                            return (dur, size, -idx)

                        best_idx, best_info = max(scan_titles.items(), key=_sort_key)
                        selected_title_index = best_idx
                        skipped = len(scan_titles) - 1
                        logger.info(
                            "Main feature selected: title %d (%s, %s) — skipping %d other title(s)",
                            best_idx, best_info.get("duration", "?"), best_info.get("size", "?"), skipped,
                        )
                        job_log.append(
                            "rip",
                            f"Scan found {len(scan_titles)} title(s). Ripping title "
                            f"{best_idx} ({best_info.get('duration', '?')}, "
                            f"{best_info.get('size', '?')}) and skipping the other "
                            f"{skipped}.",
                        )
                    else:
                        logger.warning("Disc scan returned no titles — falling back to rip all")
                        job_log.append(
                            "rip",
                            "The disc scan came back with no titles"
                            + (f" ({self._ripper.last_scan_error})"
                               if self._ripper.last_scan_error else "")
                            + ", so the main feature could not be picked before "
                            "ripping. Every title will be ripped and the longest "
                            "one kept.",
                        )
                except _SeriesDisc:
                    logger.info("Ripping every episode from the TV disc in drive %s", self.drive)
                except (subprocess.SubprocessError, OSError):
                    logger.warning("Main feature scan failed — falling back to rip all", exc_info=True)
                    job_log.append(
                        "rip",
                        "The disc scan failed, so the main feature could not be "
                        "picked before ripping. Every title will be ripped and "
                        "the longest one kept.",
                    )
            else:
                logger.info("main_feature_only is disabled — ripping all titles")
                job_log.append(
                    "rip",
                    "'Main feature only' is off, so every title on the disc will "
                    "be ripped.",
                )

            job.status = JobStatus.RIPPING
            session.commit()

            _last_rip_pct = [0.0]  # mutable container for closure
            _last_rip_commit = [0.0]
            _rip_cb_count = [0]
            _rip_commit_fails = [0]
            _rip_rate = progress.Rate()
            _rip_bytes = progress.Rate()
            # monotonic, not the wall clock: these values are only ever used as
            # differences, and an NTP step during a forty-minute rip would
            # otherwise show a negative elapsed time and a nonsense estimate.
            _rip_started_at = time.monotonic()
            _raw_dir = self._config.raw_path / str(job.id)

            def on_rip_progress(info: dict) -> None:
                _rip_cb_count[0] += 1
                p = min(info.get("overall", 0.0), 0.995)

                if _rip_cb_count[0] == 1:
                    logger.info("Rip progress callback first invocation for job %s: overall=%.4f info=%s", job.id, p, info)

                if p < _last_rip_pct[0]:
                    return  # ignore backwards jumps

                now = time.monotonic()
                if now - _last_rip_commit[0] < 2.0:
                    return

                _last_rip_pct[0] = p

                # How long is left, and how fast the disc is actually being
                # read. MakeMKV reports a position, never a rate, so both are
                # derived here — see adr/progress.py for why the estimate is
                # measured over a recent window and stays silent until it can
                # say something true.
                _rip_rate.update(p, now=now)
                _rip_bytes.update(progress.directory_size(_raw_dir), now=now)
                eta = _rip_rate.eta_to()
                speed = _rip_bytes.per_second()

                pi = {
                    "phase": "ripping",
                    "title_current": info.get("title_current", 0),
                    "title_total": info.get("title_total", 0),
                    "title_progress": round(info.get("title_progress", 0.0) or 0.0, 4),
                    "description": info.get("description", ""),
                    "eta_seconds": eta,
                    "bytes_per_second": round(speed) if speed else None,
                    "elapsed_seconds": max(0, int(now - _rip_started_at)),
                }

                # Retry commit up to 2 times on lock errors
                for attempt in range(3):
                    try:
                        job.progress_rip = p
                        job.progress_info = json.dumps(pi)
                        session.commit()
                        _last_rip_commit[0] = now
                        if _rip_cb_count[0] <= 3 or int(p * 100) % 10 == 0:
                            logger.debug("Rip progress committed for job %s: %.1f%%", job.id, p * 100)
                        break
                    except SAOperationalError as exc:
                        _rip_commit_fails[0] += 1
                        try:
                            session.rollback()
                        except Exception:
                            logger.debug("Rollback failed during rip progress retry", exc_info=True)
                        if attempt < 2:
                            time.sleep(0.1 * (attempt + 1))
                        else:
                            logger.warning(
                                "Rip progress commit failed for job %s (attempt %d, total fails %d): %s",
                                job.id, attempt + 1, _rip_commit_fails[0], exc,
                            )

            job_log.append(
                "rip",
                f"Ripping from {self._config.drive_display(self.drive)} "
                + (f"(title {selected_title_index})" if selected_title_index is not None
                   else "(every title)"),
            )
            self._ripper.log_sink = job_log.sink("rip")
            try:
                rip_result = self._ripper.rip(
                    drive_letter=self.drive,
                    job_id=job.id,
                    progress_callback=on_rip_progress,
                    title_index=selected_title_index,
                )
            finally:
                self._ripper.log_sink = None
            job_log.append(
                "rip",
                f"Rip finished: {len(rip_result.mkv_files)} file(s)."
                if rip_result.success else f"Rip failed: {rip_result.error}",
            )

            # Check if cancelled during rip
            session.refresh(job)
            if job.status == JobStatus.CANCELLED:
                logger.info("Job %s cancelled during rip", job.id)
                job.completed_at = utcnow()
                session.commit()
                return

            if not rip_result.success:
                job.status = JobStatus.ERROR
                job.error_message = rip_result.error
                job.completed_at = utcnow()
                session.commit()
                logger.error("Rip failed for job %s: %s", job.id, rip_result.error)
                Notifier(self._config).job_failed(job)
                return

            job.progress_rip = 1.0
            job.status = JobStatus.RIPPED
            job.rip_completed_at = utcnow()
            session.commit()

            # Between here and the ENCODING write below sit the eject — a
            # synchronous ioctl that blocks while the drive spins down — and a
            # commit per track. Someone watching the dashboard sees 'ripped'
            # and can press Cancel in that window, and the cancel endpoint has
            # nothing left to kill, so the only thing that carries the decision
            # is the row. Writing over it started the encode anyway and the job
            # completed as if nothing had been asked.
            if _cancelled(session, job):
                logger.info("Job %s cancelled after the rip finished", job.id)
                job_log.append("rip", "Cancelled after the rip finished.")
                return

            # 4. Eject disc (so user can insert next)
            if self._config.should_eject(self.drive):
                logger.info("Ejecting drive %s", self.drive)
                eject_drive(self.drive)
            else:
                logger.info("Auto-eject disabled for drive %s — skipping", self.drive)

            # 5. Create track records and queue encoding.
            # Naming lives in adr.naming: with television in the picture the
            # decision has real branches, and inline branching in the middle of
            # this method is where naming bugs live.
            if tmdb_confident:
                fallback_title, fallback_year = "", None
            else:
                from adr.utils import parse_disc_label

                # MakeMKV's own name for the disc, when blkid could not read
                # one. It has been parsed out of the CINFO records since the
                # beginning and never used, so a disc whose label blkid missed
                # came out as "Unknown - pt1.mp4" while MakeMKV had known what
                # it was the whole time. blkid times out on a busy drive, and
                # the drive is busy for the whole rip.
                label = volume_name or rip_result.disc_name or ""
                if not volume_name and rip_result.disc_name:
                    logger.info(
                        "blkid gave no label; using MakeMKV's disc name %r",
                        rip_result.disc_name,
                    )
                fallback_title, fallback_year = parse_disc_label(label)
                logger.info("Using disc label for output name: %s (%s)", fallback_title, fallback_year)
                job_log.append(
                    "encode",
                    f"TMDb had no confident match, so the name comes from the "
                    f"disc label {label!r}."
                    if label else
                    "TMDb had no confident match and the disc carries no "
                    "readable label, so the output is named 'Unknown'. Rename "
                    "it from the History page.",
                )

            # Durations first: they decide whether one of these titles is the
            # feature and the rest are extras, or whether the disc is a
            # genuinely multi-part film. Getting that wrong in the extras
            # direction hides half a film; getting it wrong the other way makes
            # Plex stack a two-minute trailer onto the end of the movie.
            durations = []
            for mkv_file in rip_result.mkv_files:
                seconds = None
                for ti_info in rip_result.title_info.values():
                    if ti_info.get("filename") == mkv_file.name:
                        seconds = parse_duration(ti_info.get("duration", "0:00:00")) or None
                        break
                durations.append(seconds)

            rip_files = list(rip_result.mkv_files)
            # Sizes as the last resort. Duration comes from MakeMKV's TINFO
            # records matched to files by name, and every step of that can
            # come back empty; a 968 MB file beside fifteen of 9 to 83 MB is
            # not ambiguous about which one is the film.
            sizes = []
            for mkv_file in rip_files:
                try:
                    sizes.append(mkv_file.stat().st_size)
                except OSError:
                    sizes.append(0)

            is_series = (job.content_type or "movie") == "series"
            main_index = feature_index(
                job, durations, sizes, self._config.main_feature_only,
            )
            if main_index is not None and pick_main_feature(durations) is None:
                logger.info(
                    "Job %s: no title stands clear of the rest, so the longest "
                    "(%s) is taken as the film", job.id, rip_files[main_index].name,
                )

            # "Main feature only" was on, and the disc still produced several
            # titles — the pre-rip scan is the only thing that could have
            # prevented that and it did not run. Honour the setting at the one
            # point still left: encode the film and nothing else. The extras
            # stay on disk as MKV, so nothing is lost, and hours of encoding
            # nobody asked for are not spent.
            if self._config.main_feature_only and not is_series:
                kept, lengths, reduced = only_the_feature(
                    rip_files, durations, main_index,
                )
                if len(kept) < len(rip_files):
                    dropped = len(rip_files) - len(kept)
                    length = lengths[0]
                    job_log.append(
                        "encode",
                        f"'Main feature only' is on, so only {kept[0].name}"
                        + (f" ({format_duration(length)})" if length else "")
                        + f" will be encoded. The other {dropped} ripped title(s) "
                        f"stay as MKV in {self._config.raw_path / str(job.id)}; "
                        "delete them from the history page when you no longer "
                        "want them.",
                    )
                    logger.info(
                        "Job %s: keeping %s and leaving %d other ripped title(s) "
                        "unencoded", job.id, kept[0].name, dropped,
                    )
                rip_files, durations, main_index = kept, lengths, reduced

            # Claim the episode numbers before the plan is built, not after
            # the tracks are queued.
            #
            # apply_to stamped series_first_episode when the disc went in, and
            # the counter only moved down there — so two drives fed discs a
            # minute apart both read the same value and both produced
            # S02E01-E04, one overwriting the other in the same season folder.
            # Claiming is one atomic step now, and the number it returns is
            # what the plan is built from.
            # Which of the ripped titles are actually episodes.
            #
            # Worked out before the numbers are claimed, because the count
            # claimed has to be the count used: a disc carrying five episodes
            # and one clip would otherwise take six numbers from series mode
            # and start the next disc one episode too high, on top of
            # misnaming its own.
            episodes_mask = None
            if is_series:
                episodes_mask = episode_mask(durations, (
                    int(getattr(self._config, "series_min_minutes", 15)) * 60,
                    int(getattr(self._config, "series_max_minutes", 75)) * 60,
                ))
                extras = [
                    (index, durations[index])
                    for index, keep in enumerate(episodes_mask) if not keep
                ]
                for index, seconds in extras:
                    job_log.append(
                        "encode",
                        f"Title {index + 1} ({format_duration(seconds)}) is not "
                        f"episode-length, so it goes to {EXTRAS_FOLDER}/ instead "
                        "of taking an episode number. Settings → Series sets "
                        "what counts as an episode.",
                    )
                if extras:
                    logger.info(
                        "Job %s: %d of %d titles are extras, not episodes",
                        job.id, len(extras), len(rip_files),
                    )

            if (job.content_type or "movie") == "series":
                claimed = seriesmode.take_episodes(
                    self._config,
                    sum(episodes_mask) if episodes_mask else len(rip_files),
                )
                if claimed is not None:
                    job.series_first_episode = claimed
                    session.commit()

            plan = plan_output(
                job, len(rip_files), fallback_title, fallback_year,
                main_index=main_index, episodes_mask=episodes_mask,
            )
            plex_folder_name = plan.folder
            if main_index is not None:
                job_log.append(
                    "encode",
                    f"Title {main_index + 1} is the main feature; the other "
                    f"{len(durations) - 1} go to {EXTRAS_FOLDER}/ as extras.",
                )
            job_log.append(
                "encode",
                f"Output: {plan.folder} ({'series' if plan.is_series else 'film'}, "
                f"{len(plan.filenames)} file(s))",
            )

            # Encode to local disk when the destination is network storage, so
            # HandBrake is not writing over the network for the whole encode —
            # the finished folder is transferred once at the end instead.
            #
            # 'The destination' means where the film will actually live, which
            # for a job bound for Plex is the library itself. Staging to local
            # disk and then crossing the network once, into the final folder, is
            # the whole point; a stop-off in completed_path would undo it.
            dest_parent, to_plex = final_destination(job, self._config)
            staging = should_stage(dest_parent, self._config.stage_locally)
            if staging:
                final_dir = dest_parent
                output_dir = unique_output_dir(self._config.staging_path / plex_folder_name)
                logger.info(
                    "Encoding to local staging %s; will transfer to %s%s when finished",
                    output_dir, final_dir, " (Plex library)" if to_plex else "",
                )
            else:
                final_dir = None
                output_dir = unique_output_dir(
                    dest_parent / plex_folder_name, merge=plan.is_series,
                )
            job.output_path = str(output_dir)

            for idx, mkv_file in enumerate(rip_files):
                duration_sec = durations[idx]

                track = Track(
                    job_id=job.id,
                    track_number=idx + 1,
                    filename=mkv_file.name,
                    size_mb=mkv_file.stat().st_size / BYTES_PER_MB,
                    duration_seconds=duration_sec,
                    status=TrackStatus.PENDING,
                )
                session.add(track)
                session.commit()

                out_name = plan.filenames[idx] if idx < len(plan.filenames) else f"{plex_folder_name} - pt{idx + 1}"
                if plan.episodes and idx < len(plan.episodes):
                    track.episode_number = plan.episodes[idx]
                    session.commit()

                self._encode_queue.put(EncodeTask(
                    job_id=job.id,
                    track_id=track.id,
                    input_path=mkv_file,
                    output_dir=output_dir,
                    output_filename=out_name,
                    final_dir=final_dir,
                    passthrough=not self._config.transcode_enabled,
                ))

            if _cancelled(session, job):
                logger.info("Job %s cancelled while its tracks were queued", job.id)
                job_log.append("encode", "Cancelled before encoding started.")
                return
            job.status = JobStatus.ENCODING
            session.commit()
            logger.info("Job %s: %d tracks queued for encoding", job.id, len(rip_files))

            # The numbers were claimed above, before the plan was built. This
            # only reports what happened.
            numbered = [n for n in (plan.episodes or []) if n is not None]
            if numbered:
                after = seriesmode.state(self._config)
                if after["active"]:
                    job_log.append(
                        "detect",
                        f"Episodes {numbered[0]}–{numbered[-1]} used. "
                        f"Next disc starts at episode {after['next_episode']}.",
                    )

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            logger.exception("Pipeline error for drive %s", self.drive)
            if job is not None:
                # Best-effort and separate from the database write below: if
                # the session is what broke, the traceback still has to land
                # somewhere the user can read it.
                with contextlib.suppress(Exception):
                    JobLog(self._config, job.id).append("detect", f"Pipeline error: {exc}")
                    JobLog(self._config, job.id).append("detect", tb)
            if job is not None and session is not None:
                try:
                    job.status = JobStatus.ERROR
                    job.error_message = f"{exc}\n\n{tb}"
                    job.completed_at = utcnow()
                    session.commit()
                    Notifier(self._config).job_failed(job)
                except Exception:
                    logger.warning("Could not record the failure of job %s", job.id, exc_info=True)
                    with contextlib.suppress(Exception):
                        session.rollback()
        finally:
            # Releasing the lock is the one thing that must happen. Everything
            # else here is cleanup; this is what keeps the drive usable.
            self._lock.release()
            if session is not None:
                with contextlib.suppress(Exception):
                    session.close()

    # -------------------------------------------------------------- #
    # Discs that are not video
    # -------------------------------------------------------------- #

    def _refuse(self, job, session, message: str) -> None:
        """Close a job we have deliberately decided not to process.

        Cancelled rather than errored: nothing went wrong, the disc simply is
        not something this installation was asked to handle, and an error would
        put a red job in the history and fire a failure notification for a
        setting the user chose on purpose.
        """
        job.status = JobStatus.CANCELLED
        job.error_message = message
        job.completed_at = utcnow()
        session.commit()
        logger.info("Job %s: %s", job.id, message)
        JobLog(self._config, job.id).append("detect", message)
        if self._config.should_eject(self.drive):
            eject_drive(self.drive)

    def _run_audio_cd(self, job, session, disc) -> None:
        """Rip an audio CD: identify at MusicBrainz, extract, encode, tag."""
        if not self._config.audio_cd_enabled:
            self._refuse(job, session, (
                "This is an audio CD, and audio CD ripping is turned off under "
                "Settings. The disc was left alone."
            ))
            return

        toc = disc.toc
        if toc is None or not toc.audio_tracks:
            self._refuse(job, session, (
                "The disc looked like an audio CD but its table of contents "
                "could not be read a second time. Try it again."
            ))
            return

        log = JobLog(self._config, job.id)
        album = musicbrainz.lookup(toc)
        log.append("detect", f"MusicBrainz: {album.display}")
        if album.identified:
            job.title = f"{album.artist} — {album.album}" if album.artist else album.album
            job.year = album.year
        if not job.disc_label:
            job.disc_label = album.display
        # Committed before the cancel check, not after: _cancelled() refreshes
        # the row, and a refresh throws away pending changes — the title and
        # year just set would silently vanish for every album, cancelled or
        # not.
        session.commit()
        if _cancelled(session, job):
            log.append("rip", "Cancelled before the rip started.")
            return
        job.status = JobStatus.RIPPING
        session.commit()

        ripper = AudioCDRipper(self._config, process_registry=process_registry)
        ripper.log_sink = log.sink("rip")
        try:
            cancel_check = {"at": 0.0, "value": False}

            def _audio_cancelled() -> bool:
                # Throttled the same way the ISO path throttles it: the loop
                # asks between every track and a database read per question
                # would contend with the progress commits.
                now = time.time()
                if now - cancel_check["at"] >= 2.0:
                    cancel_check["at"] = now
                    cancel_check["value"] = process_registry.is_cancelled(job.id)
                return cancel_check["value"]

            result = ripper.rip(
                device=self.drive,
                job_id=job.id,
                toc=toc,
                album=album,
                output_root=self._config.music_path,
                progress_callback=_progress_committer(job, session, "ripping"),
                should_cancel=_audio_cancelled,
            )
        finally:
            ripper.log_sink = None

        # Cancel first, as the video and ISO paths already do. Killing
        # cdparanoia makes the rip return unsuccessful, and without this the
        # job someone deliberately stopped was recorded as an ERROR and sent a
        # "job failed" notification — a red row and a phone alert for a button
        # they pressed themselves.
        if _cancelled(session, job):
            log.append("rip", "Cancelled while ripping the CD.")
            logger.info("Job %s cancelled during the audio CD rip", job.id)
            return

        if not result.success:
            job.status = JobStatus.ERROR
            job.error_message = result.error
            job.completed_at = utcnow()
            session.commit()
            log.append("rip", f"Audio CD failed: {result.error}")
            logger.error("Audio CD rip failed for job %s: %s", job.id, result.error)
            Notifier(self._config).job_failed(job)
            return

        for index, path in enumerate(result.files, start=1):
            try:
                size_mb = path.stat().st_size / BYTES_PER_MB
            except OSError:
                size_mb = 0.0
            session.add(Track(
                job_id=job.id,
                track_number=index,
                filename=path.name,
                # The path, not just the name. cleanup.job_files reads this to
                # answer "what would deleting this job remove", and its only
                # fallback is finished_files(), which accepts .mp4 and .mkv —
                # so an album of FLACs listed as nothing at all, and the
                # confirmation dialog said no files were found while three
                # gigabytes sat on the disk.
                output_path=str(path),
                size_mb=size_mb,
                status=TrackStatus.DONE,
            ))
        session.commit()

        job.progress_rip = 1.0
        # There is no encode phase, and a progress bar frozen at 40% for the
        # rest of time reads as a hung job.
        job.progress_encode = 1.0
        job.output_path = str(result.output_dir) if result.output_dir else None
        job.status = JobStatus.DONE
        job.rip_completed_at = utcnow()
        job.completed_at = utcnow()
        # A CD with one unreadable track still counts as done; the caveat is
        # recorded so the history says which tracks are missing.
        job.error_message = result.error
        session.commit()
        log.append("done", f"{len(result.files)} track(s) written to {result.output_dir}")
        logger.info("Job %s: audio CD finished — %d track(s)", job.id, len(result.files))

        if self._config.should_eject(self.drive):
            eject_drive(self.drive)
        Notifier(self._config).job_done(job, str(result.output_dir or ""))

    def _run_data_disc(self, job, session, disc) -> None:
        """Back a data disc up as an ISO image."""
        if not self._config.data_disc_enabled:
            self._refuse(job, session, (
                "This is a data disc, and disc imaging is turned off under "
                "Settings. The disc was left alone."
            ))
            return

        log = JobLog(self._config, job.id)

        # The same destination gate every video disc passes. The data-disc
        # branch forks off before destination_blocker runs, so with the NAS
        # unmounted a video disc was refused up front while a data disc wrote
        # an 8 GB image onto the container's own root disk through the dead
        # mountpoint — filling the disk SQLite lives on, which takes every
        # other running job down with it.
        from adr.storage import check_destination

        iso_dir = str(self._config.data_disc_path or "")
        # The folder legitimately does not exist until the first data disc;
        # creating it is not the risk the gate exists for. What the gate is
        # for is the parent being a dead mountpoint or an unwritable share.
        with contextlib.suppress(OSError):
            Path(iso_dir).mkdir(parents=True, exist_ok=True)
        ok, why = check_destination(
            iso_dir, require_mount=self._config.require_completed_mount,
        )
        if not ok:
            self._refuse(job, session, (
                f"This is a data disc, and the ISO folder is not usable: {why} "
                "Fix the destination under Settings or Storage and press Rip."
            ))
            return

        # Stale .iso.part files are a previous run's death, and this is the
        # first moment anyone is looking at the ISO folder again.
        isobackup.sweep_stale_parts(iso_dir)

        if not job.title:
            job.title = job.disc_label or "Data disc"
        job.status = JobStatus.RIPPING
        session.commit()

        # is_cancelled opens a database session, and the image loop runs sixteen
        # times a megabyte. Asking every two seconds is responsive enough for a
        # cancel button and cheap enough to be free.
        cancel_check = {"at": 0.0, "value": False}

        def cancelled() -> bool:
            now = time.time()
            if now - cancel_check["at"] >= 2.0:
                cancel_check["at"] = now
                cancel_check["value"] = process_registry.is_cancelled(job.id)
            return cancel_check["value"]

        result = isobackup.create_image(
            device=self.drive,
            destination_dir=self._config.data_disc_path,
            label=job.disc_label,
            progress_callback=_progress_committer(job, session, "imaging"),
            should_cancel=cancelled,
        )

        session.refresh(job)
        if job.status == JobStatus.CANCELLED:
            job.completed_at = utcnow()
            session.commit()
            # The copy checks for cancellation every two seconds, so a cancel
            # in the last window can land after the image finished — complete,
            # on disk, referenced by no track row, while this log line claimed
            # it was deleted. A cancel is a cancel: the file goes, whichever
            # side of the finish line it was on.
            if result.success and result.path:
                with contextlib.suppress(OSError):
                    Path(result.path).unlink()
            log.append("rip", "Cancelled; the image was deleted.")
            return

        if not result.success:
            job.status = JobStatus.ERROR
            job.error_message = result.error
            job.completed_at = utcnow()
            session.commit()
            log.append("rip", f"Imaging failed: {result.error}")
            logger.error("ISO backup failed for job %s: %s", job.id, result.error)
            Notifier(self._config).job_failed(job)
            return

        session.add(Track(
            job_id=job.id,
            track_number=1,
            filename=result.path.name if result.path else "disc.iso",
            # Same reason as the audio tracks above, and more so here:
            # job.output_path for an ISO job is the image *file*, and
            # finished_files() of a file is empty by definition. Without this
            # an eight-gigabyte image was invisible to the delete preview.
            output_path=str(result.path) if result.path else None,
            size_mb=result.size_bytes / BYTES_PER_MB,
            status=TrackStatus.DONE,
        ))
        job.progress_rip = 1.0
        job.progress_encode = 1.0
        job.output_path = str(result.path) if result.path else None
        job.status = JobStatus.DONE
        job.rip_completed_at = utcnow()
        job.completed_at = utcnow()
        session.commit()
        log.append("done", f"Image written to {result.path} ({result.size_bytes / BYTES_PER_MB:.0f} MB)")
        logger.info("Job %s: disc image finished — %s", job.id, result.path)

        if self._config.should_eject(self.drive):
            eject_drive(self.drive)
        Notifier(self._config).job_done(job, str(result.path or ""))


# ------------------------------------------------------------------ #
# Master orchestrator
# ------------------------------------------------------------------ #

class PipelineManager:
    """Top-level manager that wires disc detection, drive pipelines, and encoder workers."""

    def __init__(self, config: Config):
        self.config = config
        self.encode_queue: queue.Queue[EncodeTask] = queue.Queue()
        self.disc_watcher = DiscWatcher(drives=config.drives)
        self.drive_pipelines: dict[str, DrivePipeline] = {}
        self.encoder_workers: list[EncoderWorker] = []
        self.folder_watcher: FolderWatcher | None = None

    def start(self) -> None:
        """Initialise database and start all background threads."""
        init_db()

        # Sweep job logs for jobs that no longer exist, and anything past the
        # retention window. Doing it at startup means it happens without a
        # timer and without ever running mid-rip.
        try:
            session = get_session()
            try:
                joblog.prune(self.config, keep_job_ids={row[0] for row in session.query(Job.id).all()})
            finally:
                session.close()
        except Exception:
            logger.warning("Could not prune job logs at startup", exc_info=True)

        # Discover drives
        from adr.disc import list_optical_drives
        if self.config.drives == "auto":
            drives = [d["drive"] for d in list_optical_drives()]
            if not drives:
                logger.warning("No optical drives detected! Waiting for drives to appear...")
                drives = []
        else:
            drives = self.config.drives if isinstance(self.config.drives, list) else [self.config.drives]

        # Store all discovered drives (including disabled) for the UI
        self.all_drives = list(drives)

        # Create per-drive pipelines — always register callback
        # (disabled check happens at callback time for runtime toggle support)
        for drive in drives:
            pipeline = DrivePipeline(drive, self.config, self.encode_queue)
            self.drive_pipelines[drive] = pipeline
            self.disc_watcher.on_disc_inserted(pipeline.handle_disc_inserted)
            logger.info("Pipeline registered for drive %s", drive)

        # Start encoder workers
        num_workers = max(1, self.config.max_encode_jobs)
        for i in range(num_workers):
            worker = EncoderWorker(self.config, self.encode_queue, name=f"EncoderWorker-{i}")
            worker.start()
            self.encoder_workers.append(worker)

        # Close out anything the last shutdown interrupted. After the workers
        # exist, because an encode this queues has to be picked up; before the
        # disc watcher, so a job left mid-rip is already failed by the time a
        # disc still sitting in that drive is noticed.
        outcome = recovery.recover_interrupted_jobs(self.config, self.encode_queue)
        if outcome["resumed"] or outcome["failed"]:
            logger.info(
                "Interrupted jobs: %d resumed, %d closed as failed",
                len(outcome["resumed"]), len(outcome["failed"]),
            )

        # Register callback for dynamically discovered drives
        self.disc_watcher.on_new_drive(self._handle_new_drive)

        # Start watching for discs
        self.disc_watcher.start()

        # Start folder watcher (if configured)
        if self.config.watch_path:
            self.folder_watcher = FolderWatcher(
                config=self.config,
                encode_queue=self.encode_queue,
                poll_interval=self.config.watch_interval,
            )
            self.folder_watcher.start()
            logger.info("FolderWatcher enabled: %s", self.config.watch_path)
        else:
            logger.info("FolderWatcher disabled (no watch_path configured)")

        logger.info("PipelineManager started: %d drives, %d encoder workers", len(drives), num_workers)

    def _handle_new_drive(self, drive_letter: str) -> None:
        """Hot-add a newly discovered optical drive.

        Called by DiscWatcher when a drive letter appears that wasn't
        present at startup (e.g. USB DVD drive plugged in, or Windows
        mounting a new letter when a disc is inserted).
        """
        if drive_letter in self.drive_pipelines:
            return  # already known

        logger.info("Hot-adding new drive %s", drive_letter)

        # Track it for the UI
        if drive_letter not in self.all_drives:
            self.all_drives.append(drive_letter)

        pipeline = DrivePipeline(drive_letter, self.config, self.encode_queue)
        self.drive_pipelines[drive_letter] = pipeline
        self.disc_watcher.on_disc_inserted(pipeline.handle_disc_inserted)
        logger.info("Pipeline registered for hot-added drive %s", drive_letter)

    @staticmethod
    def _busy_job(drive: str) -> dict | None:
        """The job holding *drive*, for a refusal that names it. Never raises."""
        try:
            from adr.models import ACTIVE_STATUSES, Job, get_session

            session = get_session()
            try:
                job = (
                    session.query(Job)
                    .filter(Job.drive == drive, Job.status.in_(ACTIVE_STATUSES))
                    .order_by(Job.id.desc())
                    .first()
                )
                return {"id": job.id, "status": job.status.value} if job else None
            finally:
                session.close()
        except Exception:                          # noqa: BLE001 - never fatal
            logger.debug("Could not name the job holding %s", drive, exc_info=True)
            return None

    def rip_now(self, drive: str) -> tuple[bool, str]:
        """Rip the disc that is already sitting in the drive.

        The watcher only fires on the *transition* from empty to loaded, which
        is right for unattended use and useless after a failure: the disc is
        still there, nothing changes, and no amount of waiting starts it again.
        Ejecting and reinserting works — but asking someone to walk to the
        machine to re-trigger software is not a fix.
        """
        from adr.disc import _blkid_label, media_status

        # By the name its owner gave it. Someone with an Internal and an
        # External does not think in device nodes, and every one of the
        # sentences below is read on the dashboard rather than in a log.
        display = getattr(self.config, "drive_display", None)
        shown = display(drive) if callable(display) else drive

        pipeline = self.drive_pipelines.get(drive)
        if pipeline is None:
            return False, f"{shown} is not a drive this instance watches."
        if normalize_drive(drive) in self.config.disabled_drives:
            return False, f"{shown} is disabled under Settings."
        if pipeline.is_busy:
            # Name the job, because "already ripping" right after pressing
            # Cancel reads as the application being wrong. Usually it is not:
            # the tool takes a moment to die and the drive is genuinely still
            # held. Saying which job, and that Cancel is the way out, is the
            # difference between a wrong answer and a slow one.
            busy = self._busy_job(drive)
            if busy:
                return False, (
                    f"{shown} is still working on job {busy['id']} "
                    f"({busy['status']}). Cancel it on the dashboard first, or "
                    "give it a few seconds to stop."
                )
            return False, (
                f"{shown} is still finishing the last job. Give it a few "
                "seconds and try again."
            )
        # In the drive's own words. "No readable disc" covered an empty tray,
        # an open tray, a missing device node and a cgroup denial with one
        # sentence, and only one of the four is fixed by putting a disc in.
        state = media_status(drive, shown)
        if not state["ready"]:
            return False, state["detail"]

        # The same gate the rip itself would hit thirty seconds from now.
        # Letting it start anyway produces a red job saying what could have
        # been said here — and pressing Rip again produces another one.
        blocked = preflight.destination_blocker(self.config)
        if blocked:
            return False, f"Ripping would fail: {blocked}"

        label = _blkid_label(drive)
        logger.info("Manual rip requested for %s (label=%s)", drive, label)
        pipeline.handle_disc_inserted(drive, label, manual=True)
        return True, f"Started ripping {label or 'the disc'} in {shown}."

    def rescan_drives(self) -> dict:
        """Re-detect optical drives now, and hot-add any that are new.

        The watcher would find them on its own within a poll cycle, but "press
        the button and see the answer" is the whole point — a rescan that takes
        effect in thirty seconds is indistinguishable from one that did nothing.
        """
        from adr.drivetest import rescan_drives as _scan

        result = _scan()
        found = self.disc_watcher.refresh_drives()
        added = [d for d in found if d not in self.drive_pipelines]
        for drive in added:
            self._handle_new_drive(drive)

        result["added"] = added
        result["known"] = sorted(self.drive_pipelines.keys())
        return result

    def stop(self) -> None:
        """Shut down all background threads gracefully."""
        self.disc_watcher.stop()
        if self.folder_watcher:
            self.folder_watcher.stop()
        for worker in self.encoder_workers:
            worker.stop()
        for worker in self.encoder_workers:
            worker.join(timeout=5)
        logger.info("PipelineManager stopped")

    def get_status(self) -> dict[str, Any]:
        """Return current system status for the API."""
        return {
            "drives": list(self.drive_pipelines.keys()),
            "encode_queue_size": self.encode_queue.qsize(),
            "encoder_workers": len(self.encoder_workers),
            "watch_folder": {
                "enabled": self.folder_watcher is not None and self.folder_watcher.enabled,
                "path": str(self.config.watch_path) if self.config.watch_path else None,
                "output_path": str(self.folder_watcher.watch_output_path) if self.folder_watcher else None,
            },
        }
