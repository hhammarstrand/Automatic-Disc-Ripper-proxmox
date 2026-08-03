"""Pipeline orchestrator for Automatic Disc Ripper.

Coordinates the full workflow per drive: detect → identify → rip → eject → encode.
Each optical drive gets its own DrivePipeline thread. Encoding jobs are dispatched
to a shared EncoderWorker pool so ripping can continue on other drives while
encoding runs.
"""

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

from adr import joblog
from adr.config import Config
from adr.disc import DiscWatcher, eject_drive
from adr.encoder import HandBrakeEncoder
from adr.identify import identify_disc
from adr.joblog import JobLog
from adr.models import Job, JobStatus, Track, TrackStatus, get_session, init_db
from adr.naming import plan_output, relative_folder
from adr.notify import Notifier
from adr.plex import PlexNotifier
from adr.ripper import MakeMKVRipper
from adr.series import looks_like_series, parse_series_label
from adr.storage import check_destination, should_stage
from adr.utils import (
    BYTES_PER_MB,
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
        """Kill all subprocesses for a job. Returns True if any process was killed."""
        with self._lock:
            procs = self._procs.pop(job_id, [])
        killed_any = False
        for proc in procs:
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                    logger.info("Killed subprocess for job %s (pid=%s)", job_id, proc.pid)
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


class _SeriesDisc(Exception):
    """Internal: the disc holds episodes, so skip main-feature selection.

    Control flow rather than an error — main-feature selection is a block of
    nested logic and this is the clearest way out of it without restructuring
    the whole method.
    """


def rename_job_output(job, session) -> None:
    """Rename output folder and MP4 files to Plex-style name.

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

        mp4_files = sorted(new_output.glob("*.mp4"))
        multi = len(mp4_files) > 1
        for idx, f in enumerate(mp4_files, start=1):
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
    if not label or label.upper() in _GENERIC_DISC_LABELS:
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
})


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
    if dest.exists():
        counter = 2
        while (candidate := dest.parent / f"{dest.name} ({counter})").exists():
            counter += 1
        dest = candidate

    size_mb = sum(f.stat().st_size for f in src.rglob("*") if f.is_file()) / BYTES_PER_MB
    logger.info("Transferring job %s to %s (%.0f MB)", job.id, dest, size_mb)
    started = time.monotonic()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
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
    for t in job.tracks:
        if t.output_path:
            t.output_path = str(dest / Path(t.output_path).name)
    session.commit()
    return True


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

    # Handle collision — append (2), (3), etc.
    if dest.exists():
        counter = 2
        while True:
            candidate = dest.parent / f"{dest.name} ({counter})"
            if not candidate.exists():
                dest = candidate
                break
            counter += 1

    try:
        shutil.move(str(src), str(dest))
        job.plex_path = str(dest)
        job.output_path = str(dest)

        # Update track output paths to new location
        for t in job.tracks:
            if t.output_path:
                old_track = Path(t.output_path)
                t.output_path = str(dest / old_track.name)

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
    ):
        self.job_id = job_id
        self.track_id = track_id
        self.input_path = input_path
        self.output_dir = output_dir
        self.output_filename = output_filename
        self.final_dir = final_dir


# ------------------------------------------------------------------ #
# Encoder worker (shared across all drives)
# ------------------------------------------------------------------ #

class EncoderWorker(threading.Thread):
    """Consumes EncodeTask items from a queue and transcodes them."""

    def __init__(self, config: Config, task_queue: queue.Queue, name: str = "EncoderWorker"):
        super().__init__(daemon=True, name=name)
        self._config = config
        self._queue = task_queue
        self._encoder = HandBrakeEncoder(config)
        self._encoder._process_registry = process_registry
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("%s started", self.name)
        while not self._stop_event.is_set():
            try:
                task: EncodeTask = self._queue.get(timeout=2)
            except queue.Empty:
                continue

            self._process_task(task)
            self._queue.task_done()
        logger.info("%s stopped", self.name)

    def _process_task(self, task: EncodeTask) -> None:
        session = get_session()
        try:
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

            _last_enc_pct = [0.0]  # mutable container for closure
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
            job_log.append("encode", f"Encoding {task.input_path.name} -> {task.output_filename}")
            self._encoder.log_sink = job_log.sink("encode")
            try:
                result = self._encoder.encode(
                    input_path=task.input_path,
                    output_dir=task.output_dir,
                    output_filename=task.output_filename,
                    progress_callback=on_progress,
                    job_id=task.job_id,
                )
            finally:
                # Workers are shared between jobs; a stale sink would write one
                # job's output into another job's log.
                self._encoder.log_sink = None
            job_log.append(
                "encode",
                "Encode finished." if result.success else f"Encode failed: {result.error}",
            )

            # Check if cancelled during encode
            session.refresh(job)
            if job.status == JobStatus.CANCELLED:
                logger.info("Job %s cancelled during encode", task.job_id)
                job.completed_at = utcnow()
                session.commit()
                return

            if result.success:
                track.status = TrackStatus.DONE
                track.output_path = str(result.output_path)
                logger.info("Track %s encoded: %s", track.track_number, result.output_path)
            else:
                track.status = TrackStatus.ERROR
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
                self._cleanup_raw(job.id)
                self._cleanup_watch_source(job, task)

                # The disc is done. Tell whoever walked away, and tell Plex so
                # the film is visible now rather than after the next scheduled
                # scan. Both are best-effort: the film is on disk either way.
                session.commit()
                Notifier(self._config).job_done(job, job.output_path or "")
                PlexNotifier(self._config).refresh_for(job.output_path or "")
            elif any_error and all(t.status in (TrackStatus.DONE, TrackStatus.ERROR) for t in job.tracks):
                job.status = JobStatus.ERROR
                job.error_message = "One or more tracks failed to encode"
                job.completed_at = utcnow()
                session.commit()
                Notifier(self._config).job_failed(job)

            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Error processing encode task for job %s", task.job_id)
        finally:
            session.close()

    def _cleanup_raw(self, job_id: int) -> None:
        """Remove temporary raw MKV directory for a completed job."""
        raw_dir = self._config.raw_path / str(job_id)
        if raw_dir.exists():
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

    def handle_disc_inserted(self, drive: str, volume_name: str | None) -> None:
        """Callback invoked by DiscWatcher when a disc is inserted.

        Only processes if the event is for our drive. Runs the full pipeline
        in a new thread so the watcher isn't blocked.
        """
        if normalize_drive(drive) != normalize_drive(self.drive):
            return
        # Check if drive was disabled at runtime via settings UI
        if normalize_drive(drive) in self._config.disabled_drives:
            logger.info("Drive %s is disabled — ignoring disc event", drive)
            return
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

        session = get_session()
        job: Job | None = None
        try:
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

            # 1a. Say so if this disc has been ripped before. Not a refusal —
            # re-ripping is a legitimate thing to want, and a disc label is not
            # a unique identifier — but ripping the same film twice by accident
            # is forty wasted minutes and a duplicate in the library.
            previous = find_previous_rip(job, session)
            if previous:
                job.duplicate_of = previous.id
                session.commit()
                logger.warning(
                    "Disc '%s' was already ripped as job %s (%s) — ripping anyway",
                    volume_name, previous.id, previous.display_title,
                )
                JobLog(self._config, job.id).append(
                    "detect",
                    f"This disc was already ripped as job {previous.id} "
                    f"({previous.display_title}) on "
                    f"{previous.completed_at:%Y-%m-%d}." if previous.completed_at else
                    f"This disc was already ripped as job {previous.id}.",
                )

            # 1b. Fail fast if the finished files have nowhere to go. A rip
            # takes tens of minutes and several GB; discovering at the end that
            # the NAS was never mounted wastes the entire run.
            dest_ok, dest_err = check_destination(
                self._config.completed_path,
                require_mount=self._config.require_completed_mount,
            )
            # The Plex library is a real destination too — with auto_move_to_plex
            # it is the one this job will most likely use — so a broken library
            # path must fail here, not after the encode.
            if dest_ok and self._config.plex_path:
                dest_ok, dest_err = check_destination(
                    self._config.plex_path,
                    require_mount=self._config.require_completed_mount,
                )
                if not dest_ok:
                    dest_err = f"Plex library unusable: {dest_err}"
            # When encoding is staged locally, the scratch area needs room too —
            # otherwise the rip only fails later, at the staging step.
            if dest_ok and should_stage(
                self._config.plex_path or self._config.completed_path,
                self._config.stage_locally,
            ):
                dest_ok, dest_err = check_destination(self._config.staging_path)
                if not dest_ok:
                    dest_err = f"Local staging area unusable: {dest_err}"
            if not dest_ok:
                job.status = JobStatus.ERROR
                job.error_message = dest_err
                job.completed_at = utcnow()
                session.commit()
                logger.error("Job %s aborted before ripping: %s", job.id, dest_err)
                Notifier(self._config).job_failed(job)
                return

            # 2. Identify disc via TMDb
            tmdb_confident = False
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

            # 3. Prepare rip
            # Smart main-feature selection: scan disc first, pick longest title
            selected_title_index = None
            if self._config.main_feature_only:
                try:
                    logger.info("Main feature mode: scanning disc to find longest title...")
                    scan_titles = self._ripper.scan_disc(self.drive)
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
                    verdict = looks_like_series(scan_titles, self._config)
                    if verdict["is_series"] and self._config.series_detection:
                        job.content_type = "series"
                        guess = parse_series_label(job.disc_label or "")
                        job.series_season = guess["season"] or 1
                        job.series_first_episode = 1
                        session.commit()
                        logger.info(
                            "Job %s looks like a TV disc: %s", job.id, verdict["reason"],
                        )
                        job_log_early = JobLog(self._config, job.id)
                        job_log_early.append("detect", verdict["reason"])
                        job_log_early.append(
                            "detect",
                            f"Assuming season {job.series_season} starting at episode 1. "
                            "Change it in the web UI before encoding starts.",
                        )
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
                    else:
                        logger.warning("Disc scan returned no titles — falling back to rip all")
                except _SeriesDisc:
                    logger.info("Ripping every episode from the TV disc in drive %s", self.drive)
                except (subprocess.SubprocessError, OSError):
                    logger.warning("Main feature scan failed — falling back to rip all", exc_info=True)
            else:
                logger.info("main_feature_only is disabled — ripping all titles")

            job.status = JobStatus.RIPPING
            session.commit()

            _last_rip_pct = [0.0]  # mutable container for closure
            _last_rip_commit = [0.0]
            _rip_cb_count = [0]
            _rip_commit_fails = [0]

            def on_rip_progress(info: dict) -> None:
                _rip_cb_count[0] += 1
                p = min(info.get("overall", 0.0), 0.995)

                if _rip_cb_count[0] == 1:
                    logger.info("Rip progress callback first invocation for job %s: overall=%.4f info=%s", job.id, p, info)

                if p < _last_rip_pct[0]:
                    return  # ignore backwards jumps

                now = time.time()
                if now - _last_rip_commit[0] < 2.0:
                    return

                _last_rip_pct[0] = p

                pi = {
                    "phase": "ripping",
                    "title_current": info.get("title_current", 0),
                    "title_total": info.get("title_total", 0),
                    "title_progress": round(info.get("title_progress", 0.0) or 0.0, 4),
                    "description": info.get("description", ""),
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

            job_log = JobLog(self._config, job.id)
            job_log.append("rip", f"Ripping from {self.drive} (title {selected_title_index})")
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
                fallback_title, fallback_year = parse_disc_label(volume_name or "")
                logger.info("Using disc label for output name: %s (%s)", fallback_title, fallback_year)

            plan = plan_output(job, len(rip_result.mkv_files), fallback_title, fallback_year)
            plex_folder_name = plan.folder
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
                output_dir = unique_output_dir(dest_parent / plex_folder_name)
            job.output_path = str(output_dir)

            for idx, mkv_file in enumerate(rip_result.mkv_files):
                # Try to find duration from title_info by matching filename
                duration_sec = None
                for ti_info in rip_result.title_info.values():
                    if ti_info.get("filename") == mkv_file.name:
                        duration_sec = parse_duration(ti_info.get("duration", "0:00:00")) or None
                        break

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
                ))

            job.status = JobStatus.ENCODING
            session.commit()
            logger.info("Job %s: %d tracks queued for encoding", job.id, len(rip_result.mkv_files))

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            logger.exception("Pipeline error for drive %s", self.drive)
            if job:
                try:
                    job.status = JobStatus.ERROR
                    job.error_message = f"{exc}\n\n{tb}"
                    job.completed_at = utcnow()
                    session.commit()
                    Notifier(self._config).job_failed(job)
                except Exception:
                    session.rollback()
        finally:
            self._lock.release()
            session.close()


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
