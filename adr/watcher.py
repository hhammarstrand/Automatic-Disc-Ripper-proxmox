"""Watch folder scanner for Automatic Disc Ripper.

Monitors a directory for new video files and queues them for
HandBrake encoding using the standard preset. This works
independently from the disc-ripping pipeline.
"""

import logging
import os
import threading
import time
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from adr.config import Config
from adr.models import Job, JobStatus, Track, TrackStatus, get_session
from adr.utils import BYTES_PER_MB, make_plex_folder_name, utcnow

logger = logging.getLogger(__name__)

# File extensions we pick up from the watch folder
VIDEO_EXTENSIONS = {
    ".mkv", ".avi", ".mp4", ".m4v", ".mov", ".wmv", ".flv",
    ".ts", ".mpg", ".mpeg", ".m2ts", ".mts",       # MPEG transport streams / Blu-ray
    ".vob",                                          # DVD VOB files
    ".webm", ".ogv", ".3gp", ".divx",               # Web / legacy formats
    ".iso",                                          # DVD/Blu-ray ISO images
}

# Minimum age in seconds before a file is considered "complete"
# (avoids picking up files still being copied/written)
MIN_FILE_AGE = 10

# Marker suffix added to files currently being processed
_PROCESSING_SUFFIX = ".adr-processing"


class FolderWatcher(threading.Thread):
    """Scans a watch folder for new video files and queues them for encoding.

    Files are detected by polling. Once a file is found and stable (not
    being written to), it is:

    1. Renamed with a .adr-processing suffix to prevent re-pickup
    2. Registered as a Job + Track in the database
    3. Queued for HandBrake encoding via the shared encode queue
    4. After encoding, the source file is deleted

    The output goes to the configured watch_output_path (or completed_path
    as fallback).
    """

    def __init__(
        self,
        config: Config,
        encode_queue,  # queue.Queue[EncodeTask]
        poll_interval: float = 5.0,
    ):
        super().__init__(daemon=True, name="FolderWatcher")
        self._config = config
        self._encode_queue = encode_queue
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        # Track file sizes to detect "still being written"
        self._file_sizes: dict[str, tuple[int, float]] = {}  # path -> (size, first_seen_at)

    @property
    def watch_path(self) -> Path | None:
        p = self._config.watch_path
        return Path(p) if p else None

    @property
    def watch_output_path(self) -> Path:
        p = self._config.watch_output_path
        return Path(p) if p else self._config.completed_path

    @property
    def enabled(self) -> bool:
        wp = self._config.watch_path
        return bool(wp and wp.strip())

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if not self.enabled:
            logger.info("FolderWatcher disabled (no watch_path configured)")
            return

        watch = self.watch_path
        logger.info("FolderWatcher starting — watch_path=%s (exists=%s)", watch, watch.exists() if watch else False)

        try:
            watch.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("FolderWatcher: could not create watch directory %s: %s", watch, exc)
            return

        try:
            self.watch_output_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("FolderWatcher: could not create output directory %s: %s", self.watch_output_path, exc)
            return

        logger.info("FolderWatcher started — watching: %s", watch)
        logger.info("FolderWatcher output: %s", self.watch_output_path)

        # Restore leftover .adr-processing files from a previous crash
        self._restore_stale_processing_files(watch)

        while not self._stop_event.is_set():
            try:
                self._scan_once()
            except Exception:
                logger.exception("FolderWatcher scan error")
            self._stop_event.wait(self._poll_interval)

        logger.info("FolderWatcher stopped")

    # ------------------------------------------------------------------ #
    # Scanning logic
    # ------------------------------------------------------------------ #

    def _restore_stale_processing_files(self, watch_dir: Path) -> None:
        """Rename leftover .adr-processing files back so they get re-queued."""
        try:
            for entry in os.scandir(watch_dir):
                if entry.is_file() and entry.name.endswith(_PROCESSING_SUFFIX):
                    original_name = entry.name[: -len(_PROCESSING_SUFFIX)]
                    original_path = watch_dir / original_name
                    # The original name may be taken by a file dropped after
                    # the crash. Restoring onto it would replace the newer
                    # file with the older one, silently — so the restored copy
                    # steps aside instead, and both get encoded.
                    if original_path.exists():
                        stem, suffix = original_path.stem, original_path.suffix
                        counter = 2
                        while (candidate := watch_dir / f"{stem} ({counter}){suffix}").exists():
                            counter += 1
                        original_path = candidate
                    try:
                        Path(entry.path).rename(original_path)
                        logger.info("Restored stale file: %s → %s", entry.name, original_path.name)
                    except OSError as exc:
                        logger.warning("Could not restore %s: %s", entry.name, exc)
        except OSError as exc:
            logger.warning("Could not scan for stale processing files: %s", exc)

    def _scan_once(self) -> None:
        """Scan the watch folder for new, stable video files."""
        watch = self.watch_path
        if not watch or not watch.exists():
            logger.debug("FolderWatcher: watch path %s does not exist, skipping scan", watch)
            return

        now = time.time()
        current_files: set[str] = set()

        try:
            entries = list(os.scandir(watch))
        except OSError as exc:
            logger.warning("FolderWatcher: could not scan %s: %s", watch, exc)
            return

        for entry in entries:
            if not entry.is_file():
                continue
            path = Path(entry.path)

            # Skip non-video files and files already being processed
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if path.name.endswith(_PROCESSING_SUFFIX):
                continue

            key = str(path)
            current_files.add(key)

            try:
                size = entry.stat().st_size
            except OSError:
                continue

            if key in self._file_sizes:
                prev_size, first_seen = self._file_sizes[key]
                if size != prev_size:
                    # Still changing — update size and reset timer
                    logger.debug("FolderWatcher: %s size changed (%d → %d), resetting timer", path.name, prev_size, size)
                    self._file_sizes[key] = (size, now)
                    continue
                age = now - first_seen
                try:
                    # Size alone misses a preallocated file: a copy tool that
                    # writes into a full-size file changes no size while it
                    # writes, and picking it up mid-copy encodes half a film.
                    # The write clock has to have stopped too.
                    modified_ago = now - entry.stat().st_mtime
                except OSError:
                    continue
                if modified_ago < MIN_FILE_AGE:
                    logger.debug(
                        "FolderWatcher: %s still being written (mtime %.1fs ago)",
                        path.name, modified_ago,
                    )
                    continue
                if age < MIN_FILE_AGE:
                    # Not old enough yet
                    logger.debug("FolderWatcher: %s stable but too young (%.1fs / %ds)", path.name, age, MIN_FILE_AGE)
                    continue
                # File is stable — process it
                logger.info("FolderWatcher: %s is stable (%.1fs, %d bytes) — processing", path.name, age, size)
                self._file_sizes.pop(key, None)
                self._process_file(path)
            else:
                # First time seeing this file
                logger.info("FolderWatcher: new file spotted — %s (%d bytes), waiting for stability", path.name, size)
                self._file_sizes[key] = (size, now)

        # Clean up tracking for files that disappeared
        gone = set(self._file_sizes.keys()) - current_files
        for key in gone:
            self._file_sizes.pop(key, None)

    def _process_file(self, file_path: Path) -> None:
        """Register a watch-folder file as a job and queue for encoding."""
        logger.info("Watch folder: new file detected — %s", file_path.name)

        # Rename to prevent re-pickup.
        #
        # Refused outright when the target already exists. POSIX rename
        # replaces silently, and the target existing means an earlier file of
        # the same name is still queued or encoding — renaming onto it hands
        # two jobs one input and destroys one of the user's files without it
        # ever being encoded. Re-drops of a same-named file are likely, not
        # exotic: the first one "disappeared" from the folder (it was renamed)
        # with nothing visible to show for it yet. The new file just waits;
        # the next scan after the first job finishes picks it up.
        processing_path = file_path.with_suffix(file_path.suffix + _PROCESSING_SUFFIX)
        if processing_path.exists():
            logger.info(
                "Watch folder: %s is already being processed — leaving the new "
                "file until that job finishes", file_path.name,
            )
            return
        try:
            file_path.rename(processing_path)
        except OSError:
            logger.error("Could not rename %s for processing", file_path)
            return

        session = get_session()
        try:
            # Parse filename into Plex-friendly title + year
            from adr.utils import parse_disc_label, sanitize_filename, unique_output_dir
            parsed_title, parsed_year = parse_disc_label(file_path.stem)
            plex_title = sanitize_filename(parsed_title)
            plex_folder = make_plex_folder_name(plex_title, parsed_year)

            # Create job
            job = Job(
                disc_label=f"[watch] {file_path.name}",
                title=parsed_title,
                year=parsed_year,
                drive="watch",
                status=JobStatus.ENCODING,
                progress_rip=1.0,  # No ripping needed
                started_at=utcnow(),
            )
            session.add(job)
            session.commit()

            # Create single track
            track = Track(
                job_id=job.id,
                track_number=1,
                filename=file_path.name,
                size_mb=processing_path.stat().st_size / BYTES_PER_MB,
                status=TrackStatus.PENDING,
            )
            session.add(track)
            session.commit()

            # Queue encoding with Plex-style output: Title (Year)/Title (Year).mp4
            #
            # Staged exactly like a disc encode. The watcher predates staging
            # and never learned it, so a watch_output_path on the NAS had
            # HandBrake writing across the network for the whole encode —
            # the very thing stage_locally exists to prevent, quietly absent
            # from one of the two paths that encode.
            from adr.pipeline import EncodeTask
            from adr.storage import should_stage

            final_parent = self.watch_output_path
            if should_stage(final_parent, self._config.stage_locally):
                final_dir = final_parent
                output_dir = unique_output_dir(
                    Path(self._config.staging_path) / plex_folder,
                )
            else:
                final_dir = None
                output_dir = unique_output_dir(final_parent / plex_folder)
            job.output_path = str(output_dir)
            session.commit()

            self._encode_queue.put(EncodeTask(
                job_id=job.id,
                track_id=track.id,
                input_path=processing_path,
                output_dir=output_dir,
                output_filename=plex_folder,  # -> "Title (Year).mp4"
                final_dir=final_dir,
            ))

            logger.info("Watch folder: job #%d queued for encoding — %s -> %s", job.id, file_path.name, plex_folder)

        except (OSError, SQLAlchemyError):
            session.rollback()
            logger.exception("Failed to queue watch-folder file: %s", file_path.name)
            # Restore original filename
            try:
                processing_path.rename(file_path)
            except OSError:
                logger.warning("Could not restore original filename %s after processing failure", file_path, exc_info=True)
        finally:
            session.close()
