"""Rip an audio CD to tagged FLAC or MP3.

Audio CDs have nothing in common with the video path: there is no MakeMKV, no
HandBrake, no title selection and no TMDb. Digital audio extraction is its own
problem — the format has no error correction worth the name, so a scratch does
not produce a read error, it produces a click. cdparanoia exists because of
this: it re-reads and overlaps until the samples agree, which is why it is the
tool here rather than a plain read of the device.

The flow per track is extract to WAV with cdparanoia, encode with ffmpeg,
delete the WAV. Encoding as we go rather than at the end means a 700 MB CD
never needs more than one track's worth of scratch space.

Output is laid out the way every music server expects::

    Artist/Album (Year)/01 - Track title.flac

A CD MusicBrainz has never seen still rips; it is filed under its disc ID,
which is stable, so the same disc always lands in the same place.
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from adr.config import Config
from adr.disctype import Toc, TocTrack
from adr.musicbrainz import AlbumInfo
from adr.utils import kill_process_tree, sanitize_filename

logger = logging.getLogger(__name__)

#: A single track, however scratched, should not hold a drive for half an hour.
TRACK_TIMEOUT = 1800

#: Encoding is CPU-bound and predictable; a stuck ffmpeg is a bug, not a disc.
ENCODE_TIMEOUT = 900

#: cdparanoia's progress line: "(== PROGRESS == [ ... | 010304 00 ] == :^D * ==)"
_PROGRESS_RE = re.compile(r"\|\s*(\d+)\s+\d+\s*\]")

#: Sectors per second on a CD.
FRAMES_PER_SECOND = 75

SUPPORTED_FORMATS = ("flac", "mp3")


@dataclass
class AudioRipResult:
    """What came of ripping one audio CD."""

    success: bool = False
    output_dir: Path | None = None
    files: list[Path] = field(default_factory=list)
    failed_tracks: list[int] = field(default_factory=list)
    error: str | None = None


def missing_tools(config: Config) -> list[str]:
    """Return the names of audio tools that are not installed.

    Both are needed: cdparanoia does the extraction, ffmpeg the encoding.
    Reporting them together means one trip to the Doctor page rather than two.
    """
    missing = []
    if not shutil.which(config.cdparanoia_path) and not Path(config.cdparanoia_path).is_file():
        missing.append(config.cdparanoia_path)
    if not shutil.which(config.ffmpeg_path) and not Path(config.ffmpeg_path).is_file():
        missing.append(config.ffmpeg_path)
    return missing


def album_folder(album: AlbumInfo) -> Path:
    """The ``Artist/Album (Year)`` folder this album belongs in."""
    artist = sanitize_filename(album.artist) or "Unknown Artist"
    if album.identified:
        name = sanitize_filename(album.album) or "Unknown Album"
        if album.year:
            name = f"{name} ({album.year})"
    else:
        # The disc ID is the only stable name an unidentified disc has. Using
        # it means re-ripping the same CD overwrites its own folder instead of
        # piling up "Unknown Album (2)", "(3)", "(4)".
        name = f"Unidentified CD {album.disc_id}"
    return Path(artist) / name


def track_filename(number: int, title: str, extension: str) -> str:
    """``01 - Title.flac``, zero-padded so a plain sort is track order."""
    safe = sanitize_filename(title) or f"Track {number:02d}"
    return f"{number:02d} - {safe}.{extension}"


class AudioCDRipper:
    """Extracts and encodes the audio tracks of a CD."""

    def __init__(self, config: Config, process_registry=None):
        self._config = config
        self._process_registry = process_registry
        #: Optional sink for tool output, so failures are visible in the UI.
        self.log_sink: Callable[[str], None] | None = None

    # -------------------------------------------------------------- #
    # Public API
    # -------------------------------------------------------------- #

    def rip(
        self,
        device: str,
        job_id: int,
        toc: Toc,
        album: AlbumInfo,
        output_root: Path,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> AudioRipResult:
        """Rip every audio track on *toc* into *output_root*.

        Individual tracks are allowed to fail. A CD with one unreadable track
        should still yield the other eleven — throwing the whole rip away
        because of one scratch is not a trade anyone would choose.
        """
        result = AudioRipResult()

        missing = missing_tools(self._config)
        if missing:
            result.error = (
                f"Audio CD ripping needs {' and '.join(missing)}, which are not "
                "installed. Run the installer again, or apt install cdparanoia ffmpeg."
            )
            return result

        tracks = toc.audio_tracks
        if not tracks:
            result.error = "The disc's table of contents lists no audio tracks."
            return result

        extension = self._config.audio_cd_format
        if extension not in SUPPORTED_FORMATS:
            result.error = (
                f"audio_cd_format is '{extension}'; it must be one of "
                f"{', '.join(SUPPORTED_FORMATS)}."
            )
            return result

        output_dir = output_root / album_folder(album)
        scratch = self._config.raw_path / f"{job_id}-audio"
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            scratch.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result.error = f"Could not create output directories: {exc}"
            return result
        result.output_dir = output_dir

        total = len(tracks)
        for index, track in enumerate(tracks):
            title = album.title_for(track.number)
            self._report(
                progress_callback,
                overall=index / total,
                track_current=index + 1,
                track_total=total,
                description=f"Ripping track {track.number}: {title}",
            )

            wav = scratch / f"track{track.number:02d}.wav"
            try:
                extracted = self._extract(
                    device, track, toc, wav, job_id,
                    on_fraction=lambda f, i=index: self._report(
                        progress_callback,
                        overall=(i + f * 0.8) / total,
                        track_current=i + 1,
                        track_total=total,
                        description=f"Ripping track {tracks[i].number}",
                    ),
                )
                if not extracted:
                    result.failed_tracks.append(track.number)
                    continue

                self._report(
                    progress_callback,
                    overall=(index + 0.8) / total,
                    track_current=index + 1,
                    track_total=total,
                    description=f"Encoding track {track.number}: {title}",
                )
                encoded = self._encode(
                    wav, output_dir, track.number, title, album, total, job_id,
                )
                if encoded is None:
                    result.failed_tracks.append(track.number)
                    continue
                result.files.append(encoded)
            finally:
                with contextlib.suppress(OSError):
                    wav.unlink(missing_ok=True)

        with contextlib.suppress(OSError):
            scratch.rmdir()

        self._report(
            progress_callback, overall=1.0, track_current=total, track_total=total,
            description="Audio CD finished",
        )

        result.success = bool(result.files)
        if not result.success:
            result.error = (
                "No track could be read from this CD. cdparanoia reached the "
                "drive but every track failed, which usually means the disc is "
                "damaged rather than that the drive is."
            )
        elif result.failed_tracks:
            listed = ", ".join(str(n) for n in result.failed_tracks)
            result.error = f"Ripped {len(result.files)} of {total} tracks; failed: {listed}."
        return result

    # -------------------------------------------------------------- #
    # Steps
    # -------------------------------------------------------------- #

    def _extract(
        self,
        device: str,
        track: TocTrack,
        toc: Toc,
        destination: Path,
        job_id: int | None = None,
        on_fraction: Callable[[float], None] | None = None,
    ) -> bool:
        """Extract one track to WAV with cdparanoia. True on success."""
        cmd = [
            self._config.cdparanoia_path,
            "-d", device,
            "-w",
            str(track.number),
            str(destination),
        ]
        logger.info("Extracting audio track %s: %s", track.number, " ".join(cmd))

        start = track.lba
        span = max(1, int(round(toc.duration_seconds(track) * FRAMES_PER_SECOND)))

        def handle(line: str) -> None:
            match = _PROGRESS_RE.search(line)
            if not match or on_fraction is None:
                return
            sector = int(match.group(1))
            fraction = (sector - start) / span
            if 0.0 <= fraction <= 1.0:
                on_fraction(fraction)

        code, tail = self._run(cmd, TRACK_TIMEOUT, handle, job_id)
        if code != 0:
            logger.warning("cdparanoia failed on track %s (exit %s)", track.number, code)
            self._log(f"cdparanoia failed on track {track.number}: {tail}")
            with contextlib.suppress(OSError):
                destination.unlink(missing_ok=True)
            return False
        if not destination.exists() or destination.stat().st_size == 0:
            self._log(f"cdparanoia produced no data for track {track.number}")
            return False
        return True

    def _encode(
        self,
        wav: Path,
        output_dir: Path,
        number: int,
        title: str,
        album: AlbumInfo,
        total: int,
        job_id: int | None = None,
    ) -> Path | None:
        """Encode one WAV to the configured format with tags. Path on success."""
        extension = self._config.audio_cd_format
        destination = output_dir / track_filename(number, title, extension)

        cmd = [self._config.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav)]
        if extension == "flac":
            cmd += ["-c:a", "flac", "-compression_level", "8"]
        else:
            cmd += ["-c:a", "libmp3lame", "-b:a", self._config.audio_cd_mp3_bitrate]

        for key, value in (
            ("title", title),
            ("artist", album.artist),
            ("album_artist", album.artist),
            ("album", album.album),
            ("track", f"{number}/{total}"),
            ("date", str(album.year) if album.year else ""),
        ):
            if value:
                cmd += ["-metadata", f"{key}={value}"]
        cmd.append(str(destination))

        code, tail = self._run(cmd, ENCODE_TIMEOUT, None, job_id)
        if code != 0:
            logger.warning("ffmpeg failed on track %s (exit %s): %s", number, code, tail)
            self._log(f"ffmpeg failed on track {number}: {tail}")
            with contextlib.suppress(OSError):
                destination.unlink(missing_ok=True)
            return None
        return destination

    # -------------------------------------------------------------- #
    # Process plumbing
    # -------------------------------------------------------------- #

    def _run(
        self,
        cmd: list[str],
        timeout: int,
        on_line: Callable[[str], None] | None,
        job_id: int | None = None,
    ) -> tuple[int, str]:
        """Run *cmd*, streaming its output. Returns ``(exit code, last output)``.

        Output is read on a thread rather than with communicate() so progress
        arrives while the tool is working. cdparanoia rewrites its progress bar
        with carriage returns, so lines are split on both \\r and \\n.

        The child gets its own process group. Killing only the process we
        started leaves any child *it* started holding the pipe open, and then
        the reader thread blocks on a read that will never return — which turns
        a timeout, the mechanism meant to stop us hanging, into a hang.
        """
        tail: list[str] = []
        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return -1, str(exc)

        if self._process_registry is not None and job_id is not None:
            self._process_registry.register(job_id, proc)

        def reader() -> None:
            assert proc.stdout is not None
            buffer = ""
            for chunk in iter(lambda: proc.stdout.read(256), ""):
                buffer += chunk
                buffer = buffer.replace("\r", "\n")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    tail.append(line)
                    del tail[:-20]
                    if on_line is not None:
                        with contextlib.suppress(Exception):
                            on_line(line)

        thread = threading.Thread(target=reader, daemon=True, name="audiocd-reader")
        thread.start()
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            proc.wait()
            code = -1
            tail.append(f"Timed out after {timeout}s")
        finally:
            if self._process_registry is not None and job_id is not None:
                self._process_registry.unregister(job_id, proc)
            thread.join(timeout=5)
            # Closing a pipe another thread is blocked reading from waits on
            # that read. With the whole process group gone the reader has
            # already finished; if somehow it has not, leaking one descriptor
            # is a far better outcome than blocking this thread for good.
            if not thread.is_alive():
                with contextlib.suppress(OSError):
                    if proc.stdout:
                        proc.stdout.close()
            else:
                logger.warning("Output reader for %s did not stop; leaving its pipe open", cmd[0])
        return code, " | ".join(tail[-5:])

    def _report(self, callback, **payload) -> None:
        if callback is None:
            return
        with contextlib.suppress(Exception):
            callback(payload)

    def _log(self, message: str) -> None:
        if self.log_sink is not None:
            with contextlib.suppress(Exception):
                self.log_sink(message)
