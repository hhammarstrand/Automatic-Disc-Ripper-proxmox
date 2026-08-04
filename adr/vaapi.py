"""Encode on the GPU with ffmpeg, when HandBrake cannot.

A container can have a working GPU and a HandBrake that cannot reach it. The
render node is passed through, the Intel driver loads, ``vainfo`` lists encode
profiles — and HandBrake still says ``encqsvInit: qsv is not available on the
system`` for every title of every disc, because its Quick Sync path goes
through the Intel Media SDK rather than through VA-API, and the Media SDK is
deprecated and no longer matches current drivers.

VA-API is the same hardware by a different road, and ffmpeg drives it
directly. ffmpeg is already installed for audio CDs, so this costs nothing but
the code below: hand it the render node, upload the frames, and let the GPU's
fixed-function encoder do the work it was already capable of.

What it gives up against HandBrake is presets. HandBrake's are a large body of
tuning, and none of it transfers. So this offers what VA-API actually exposes
— a codec, a quality number, and a resolution cap — and says so rather than
pretending to be a preset system.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from adr.encoder import EncodeResult
from adr.utils import BYTES_PER_MB

logger = logging.getLogger(__name__)

#: The codecs VA-API exposes, and the ffmpeg encoder for each. H.264 is first
#: because every Intel GPU with an encode engine has it; HEVC arrived with
#: Skylake and is the better choice where it exists.
CODECS = {
    "h264": "h264_vaapi",
    "hevc": "hevc_vaapi",
}

#: Quality, as VA-API means it: a quantiser, lower being better. 22 is close
#: to HandBrake's RF 20 for H.264 in perceived quality at a similar size.
DEFAULT_QUALITY = 22
QUALITY_RANGE = (15, 35)

#: How long to wait for a probe encode. A real encode has no timeout — a
#: two-hour film legitimately takes hours — but a probe that has not finished
#: in this long has hung.
PROBE_TIMEOUT = 120


def _device(config) -> str:
    """The render node to encode on."""
    configured = (getattr(config, "vaapi_device", "") or "").strip()
    if configured:
        return configured
    from adr import gpu

    nodes = gpu.render_nodes()
    return nodes[0] if nodes else "/dev/dri/renderD128"


#: Audio codecs MP4 can actually hold. A MakeMKV rip routinely carries
#: TrueHD or DTS-HD MA, and neither is on this list — copying one into an MP4
#: fails, and it fails at the *end*, after the whole encode has been done.
MP4_AUDIO = frozenset({"aac", "ac3", "eac3", "mp3", "alac", "opus", "flac"})

#: What an unsupported track becomes instead. AC-3 at 640 kb/s keeps 5.1
#: surround and plays on everything, which is the point of a surround rip;
#: downmixing it to stereo would throw away the reason for keeping the disc.
FALLBACK_AUDIO = ["-c:a", "ac3", "-b:a", "640k"]


def audio_streams(exe: str, path: Path) -> list[str]:
    """The codec of each audio track in *path*, in order."""
    ffprobe = re.sub(r"ffmpeg(\.exe)?$", "ffprobe", exe)
    if not (shutil.which(ffprobe) or os.path.isfile(ffprobe)):
        return []
    try:
        proc = subprocess.run(  # noqa: S603
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip().lower() for line in proc.stdout.splitlines() if line.strip()]


#: The stereo track every player can decode, and its bitrate. 192k is what
#: HandBrake's own presets use for AAC stereo.
STEREO_CODEC = "aac"
STEREO_BITRATE = "192k"


def audio_plan(exe: str, input_path: Path, output_path: Path) -> list[str]:
    """The audio mapping and codecs for one encode.

    Modelled on what HandBrake's "Surround" presets produce, because that is
    what someone coming from one expects and because the shape is right:
    **an AAC stereo track first, then the original surround track.**

    Copying the disc's AC-3 straight through and stopping there is what this
    used to do, and it is a track a lot of hardware will not decode from an
    MP4 — a TV, a phone, a browser. The film plays with no sound and nothing
    anywhere says why. The stereo track is the guarantee that something comes
    out of the speakers; the surround track is there for whatever can use it.

    Every source track is kept, not just one. A Swedish disc carries Swedish
    and English, and picking for the user would be choosing which language
    they are allowed.

    MKV holds anything, so there it is a straight copy.
    """
    if output_path.suffix.lower() != ".mp4":
        return ["-map", "0:a?", "-c:a", "copy"]

    codecs = audio_streams(exe, input_path)
    if not codecs:
        # Nothing could be read about the audio. AC-3 for everything is the
        # safe guess: it costs quality on a track that might have copied
        # fine, where the other way round costs the entire encode.
        return ["-map", "0:a?", *FALLBACK_AUDIO]

    # Output 0 is a stereo downmix of the first source track; outputs 1..n
    # are the source tracks themselves, in order.
    args = ["-map", "0:a:0", "-map", "0:a?"]
    args += [
        "-c:a:0", STEREO_CODEC, "-ac:a:0", "2", "-b:a:0", STEREO_BITRATE,
    ]
    for index, codec in enumerate(codecs):
        out = index + 1
        if codec in MP4_AUDIO:
            args += [f"-c:a:{out}", "copy"]
        else:
            args += [f"-c:a:{out}", "ac3", f"-b:a:{out}", "640k"]
    # Without this the player picks whichever track the file happens to list
    # first, which on a multi-language disc is a coin toss over the language.
    args += ["-disposition:a:0", "default"]
    return args


def build_command(
    exe: str, input_path: Path, output_path: Path, config,
    audio: list[str] | None = None,
) -> list[str]:
    """The ffmpeg command line for one hardware encode.

    The filter chain is the part worth reading. ``format=nv12`` converts to
    the one pixel format Intel's encoder accepts, and ``hwupload`` moves the
    frame into GPU memory — without both, ffmpeg fails with an impenetrable
    "Impossible to convert between the formats" rather than anything about
    hardware. ``scale`` is applied before the upload, on the CPU, because
    scaling a frame already in GPU memory needs a different filter and the
    saving is not worth a second code path.
    """
    codec = CODECS.get(
        (getattr(config, "vaapi_codec", "") or "h264").lower(), "h264_vaapi",
    )
    quality = int(getattr(config, "vaapi_quality", DEFAULT_QUALITY) or DEFAULT_QUALITY)
    quality = max(QUALITY_RANGE[0], min(QUALITY_RANGE[1], quality))
    height = int(getattr(config, "vaapi_max_height", 0) or 0)

    chain = []
    if height:
        # -2 keeps the aspect ratio and rounds to an even width, which the
        # encoder requires. Never upscales: min() leaves a smaller source be.
        chain.append(f"scale=-2:'min({height},ih)'")
    chain += ["format=nv12", "hwupload"]

    cmd = [
        exe,
        "-hide_banner",
        "-nostdin",
        "-vaapi_device", _device(config),
        "-i", str(input_path),
        "-map", "0:v:0",
        "-vf", ",".join(chain),
        "-c:v", codec,
        "-qp", str(quality),
    ]
    # The audio plan brings its own -map entries: how many output tracks there
    # are depends on what the source holds, so the mapping and the codecs have
    # to be decided together or they drift apart.
    cmd += audio if audio is not None else ["-map", "0:a?", "-c:a", "copy"]

    if output_path.suffix.lower() == ".mp4":
        # Disc subtitles are bitmap — PGS on Blu-ray, VOBSUB on DVD — and MP4
        # holds neither. Mapping them in would abort the encode at the end.
        cmd += ["-movflags", "+faststart"]
    else:
        cmd += ["-map", "0:s?", "-c:s", "copy"]

    cmd += ["-y", str(output_path)]
    return cmd


def probe(config) -> dict:
    """Can ffmpeg encode on this GPU, right now? ``{"ok", "detail", "codecs"}``

    Asked by encoding a handful of generated frames, not by reading a list of
    encoders. ffmpeg lists ``h264_vaapi`` on any build compiled with VA-API,
    whether or not a GPU is present, so the list answers a different question
    from the one being asked.
    """
    result = {"ok": False, "detail": "", "codecs": []}
    exe = getattr(config, "ffmpeg_path", "") or "ffmpeg"
    if not (shutil.which(exe) or os.path.isfile(exe)):
        result["detail"] = f"{exe} is not installed."
        return result

    device = _device(config)
    if not os.path.exists(device):
        result["detail"] = f"{device} does not exist, so there is no GPU to encode on."
        return result

    failures = []
    for name, encoder in CODECS.items():
        cmd = [
            exe, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-vaapi_device", device,
            "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
            "-vf", "format=nv12,hwupload",
            "-c:v", encoder, "-f", "null", "-y", "-",
        ]
        try:
            proc = subprocess.run(  # noqa: S603
                cmd, capture_output=True, text=True, errors="replace",
                timeout=PROBE_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"{name}: {exc}")
            continue
        if proc.returncode == 0:
            result["codecs"].append(name)
        else:
            said = (proc.stderr or "").strip().splitlines()
            failures.append(f"{name}: {said[-1] if said else f'exit {proc.returncode}'}")

    if result["codecs"]:
        result["ok"] = True
        result["detail"] = (
            "ffmpeg encoded a test clip on " + device + " with: "
            + ", ".join(result["codecs"]) + "."
        )
    else:
        result["detail"] = (
            f"ffmpeg could not encode on {device}. " + " ".join(failures)
        )
    return result


class VaapiEncoder:
    """Transcode with ffmpeg on the GPU.

    Deliberately the same shape as ``HandBrakeEncoder`` — ``encode()`` with
    the same arguments, the same ``EncodeResult``, the same progress dict,
    ``log_sink`` and ``_process_registry`` — so the pipeline does not have to
    know which one it is holding.
    """

    def __init__(self, config):
        self._config = config
        self._exe = getattr(config, "ffmpeg_path", "") or "ffmpeg"
        self._completed_path = config.completed_path
        self._active_proc: subprocess.Popen | None = None
        self._process_registry = None
        self.log_sink: Callable[[str], None] | None = None

    @property
    def active_proc(self) -> subprocess.Popen | None:
        return self._active_proc

    def list_presets(self) -> dict[str, list[str]]:
        """There are no presets here, and saying so beats an empty dropdown."""
        return {}

    def encode(
        self,
        input_path: Path | str,
        output_dir: Path | str | None = None,
        output_filename: str | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        job_id: int | None = None,
    ) -> EncodeResult:
        result = EncodeResult()
        input_path = Path(input_path)
        result.input_path = input_path

        if not input_path.exists():
            result.error = f"Input file not found: {input_path}"
            logger.error(result.error)
            return result

        dest_dir = Path(output_dir) if output_dir else self._completed_path
        dest_dir.mkdir(parents=True, exist_ok=True)
        if output_filename:
            out_name = (
                output_filename if output_filename.endswith(".mp4")
                else f"{output_filename}.mp4"
            )
        else:
            out_name = input_path.stem + ".mp4"
        output_path = dest_dir / out_name
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result.output_path = output_path

        duration = probe_duration(self._exe, input_path)
        cmd = build_command(
            self._exe, input_path, output_path, self._config,
            audio_plan(self._exe, input_path, output_path),
        )
        # Progress on stdout in a parseable form, so stderr stays free for the
        # explanation when something goes wrong.
        cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]

        logger.info("Starting VA-API encode: %s -> %s", input_path.name, output_path)
        logger.debug("ffmpeg cmd: %s", " ".join(cmd))

        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except OSError as exc:
            result.error = f"Could not start ffmpeg: {exc}"
            logger.error(result.error)
            return result

        self._active_proc = proc
        if self._process_registry and job_id is not None:
            self._process_registry.register(job_id, proc)

        stderr_tail: list[str] = []
        drain = threading.Thread(
            target=self._drain_stderr, args=(proc, stderr_tail), daemon=True,
        )
        drain.start()

        try:
            self._read_progress(proc, duration, progress_callback)
            proc.wait()
        finally:
            drain.join(timeout=5)
            if self._process_registry and job_id is not None:
                self._process_registry.unregister(job_id, proc)
            self._active_proc = None

        if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            result.success = True
            return result

        # ffmpeg's last words explain the failure; a bare exit code sends
        # someone to a log to find the one line that mattered.
        said = _last_meaningful(stderr_tail)
        result.error = (
            f"ffmpeg exited with code {proc.returncode}"
            + (f": {said}" if said else " (no output file created)")
        )
        if _cannot_read_input(said):
            # "Invalid data found when processing input" reads as an encoder
            # fault and is not one: the file it was handed is not a playable
            # video. Almost always a rip that was stopped part-way, because
            # MakeMKV writes titles as it goes and a truncated MKV looks
            # perfectly ordinary in a directory listing.
            size = input_path.stat().st_size if input_path.exists() else 0
            result.error = (
                f"{input_path.name} is not a readable video file "
                f"({size / BYTES_PER_MB:.0f} MB on disk), so nothing could be "
                "encoded from it. This is what a rip that was stopped part-way "
                "leaves behind — MakeMKV writes each title as it goes. Put the "
                f"disc back in and rip it again. ffmpeg said: {said}"
            )
        logger.error("VA-API encode failed: %s", result.error)
        return result

    def _drain_stderr(self, proc, tail: list[str]) -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                tail.append(line)
                del tail[:-40]
                if self.log_sink:
                    self.log_sink(line[:500])
        except (OSError, ValueError):
            logger.debug("ffmpeg stderr drain ended early", exc_info=True)

    def _read_progress(self, proc, duration: float, callback) -> None:
        """Turn ffmpeg's -progress stream into the pipeline's progress dict.

        ffmpeg emits key=value lines and a `progress=continue` terminator per
        block. Without a duration there is no fraction to report, so speed and
        frame rate are sent alone rather than a fabricated percentage.
        """
        if callback is None:
            for _ in iter(proc.stdout.readline, b""):
                pass
            return

        started = time.monotonic()
        block: dict[str, str] = {}
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if "=" in line:
                    key, _, value = line.partition("=")
                    block[key.strip()] = value.strip()
                if not line.startswith("progress="):
                    continue

                out_us = _number(block.get("out_time_us") or block.get("out_time_ms"))
                seconds = out_us / 1_000_000 if out_us else 0.0
                fps = _number(block.get("fps"))
                progress = min(seconds / duration, 1.0) if duration > 0 else 0.0

                eta = 0
                elapsed = time.monotonic() - started
                if progress > 0.01 and elapsed > 5:
                    eta = int(elapsed / progress - elapsed)

                callback({
                    "progress": progress,
                    "eta_seconds": eta,
                    "fps": fps,
                    "fps_avg": fps,
                    "pass_num": 1,
                    "pass_total": 1,
                    "state": "working",
                })
                block = {}
        except (OSError, ValueError):
            logger.debug("ffmpeg progress stream ended early", exc_info=True)


def probe_duration(exe: str, path: Path) -> float:
    """How many seconds long the input is, or 0.

    ffmpeg reports elapsed output time, not a percentage, so without this
    there is no denominator and the bar cannot move. ffprobe is asked first
    because it answers in one number; ffmpeg's own banner is the fallback for
    an install that somehow has one and not the other.
    """
    ffprobe = re.sub(r"ffmpeg(\.exe)?$", "ffprobe", exe)
    if shutil.which(ffprobe) or os.path.isfile(ffprobe):
        try:
            proc = subprocess.run(  # noqa: S603
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=60,
            )
            value = _number(proc.stdout.strip())
            if value > 0:
                return value
        except (OSError, subprocess.SubprocessError, ValueError):
            logger.debug("ffprobe could not read %s", path, exc_info=True)

    try:
        proc = subprocess.run(  # noqa: S603
            [exe, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, errors="replace", timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0
    match = re.search(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", proc.stderr or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _number(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _cannot_read_input(said: str) -> bool:
    """Whether ffmpeg's complaint is about the input rather than the encode."""
    lowered = (said or "").lower()
    return (
        "invalid data found" in lowered
        or "error opening input" in lowered
        or "moov atom not found" in lowered
    )


def _last_meaningful(lines: list[str]) -> str:
    """The line of ffmpeg's output that explains the failure."""
    for line in reversed(lines):
        lowered = line.lower()
        if any(word in lowered for word in
               ("error", "failed", "invalid", "cannot", "no such", "unable")):
            return line[:300]
    return lines[-1][:300] if lines else ""
