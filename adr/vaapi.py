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


#: Two-letter language codes and the three-letter ones they mean. A disc tags
#: its tracks with ISO 639-2 ("swe"), a person types ISO 639-1 ("sv"), and
#: comparing them directly fails on exactly the pairs that matter — "sv" and
#: "swe" share only one letter.
_LANGUAGE_ALIASES = {
    "sv": "swe", "en": "eng", "no": "nor", "nb": "nor", "da": "dan",
    "fi": "fin", "is": "isl", "de": "deu", "fr": "fra", "es": "spa",
    "it": "ita", "nl": "nld", "pt": "por", "pl": "pol", "ru": "rus",
    "ja": "jpn", "zh": "zho", "ko": "kor",
}

#: The other three-letter spelling some discs use. German, French, Dutch and
#: Chinese each have two ISO 639-2 codes, and discs are not consistent.
_LANGUAGE_EQUIVALENTS = {
    "deu": {"ger"}, "ger": {"deu"},
    "fra": {"fre"}, "fre": {"fra"},
    "nld": {"dut"}, "dut": {"nld"},
    "zho": {"chi"}, "chi": {"zho"},
    "isl": {"ice"}, "ice": {"isl"},
}


def normalise_language(value: str) -> str:
    """A language code as a disc would spell it, or ""."""
    code = (value or "").strip().lower()
    return _LANGUAGE_ALIASES.get(code, code)


def language_matches(tag: str, wanted: str) -> bool:
    """Whether a track's language tag is the one asked for."""
    tag = normalise_language(tag)
    wanted = normalise_language(wanted)
    if not tag or not wanted:
        return False
    return tag == wanted or tag in _LANGUAGE_EQUIVALENTS.get(wanted, set())


def audio_streams(exe: str, path: Path) -> list[dict]:
    """Every audio track in *path*: ``{"codec", "language"}``, in order.

    The language comes along because it decides which track a person actually
    wants to hear, and a rip of a Swedish disc that defaults to the English
    track is wrong in a way no amount of encoding quality makes up for.

    CSV rather than one value per line: a track with no language tag would
    otherwise simply be missing a line, and every track after it would be
    attributed to the wrong stream.
    """
    ffprobe = re.sub(r"ffmpeg(\.exe)?$", "ffprobe", exe)
    if not (shutil.which(ffprobe) or os.path.isfile(ffprobe)):
        return []
    try:
        proc = subprocess.run(  # noqa: S603
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_name:stream_tags=language",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    streams = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = [p.strip().lower() for p in line.split(",")]
        streams.append({
            "codec": parts[0],
            "language": parts[1] if len(parts) > 1 else "",
        })
    return streams


def preferred_track(streams: list[dict], wanted: str) -> int:
    """Which track index to make the default one.

    Falls back to the first track when the language is not asked for or not
    present. Silently picking something else would be worse than the disc's
    own order, which at least someone chose.
    """
    if not wanted:
        return 0
    for index, stream in enumerate(streams):
        if language_matches(stream.get("language", ""), wanted):
            return index
    return 0


#: The stereo track every player can decode, and its bitrate.
#:
#: 160k because that is what a HandBrake "Surround" preset actually specifies
#: — read out of one rather than guessed at:
#:
#:     "AudioList": [
#:       {"AudioEncoder": "av_aac",   "AudioMixdown": "stereo",   "AudioBitrate": 160},
#:       {"AudioEncoder": "copy:ac3", "AudioMixdown": "7point1",  "AudioBitrate": 640}
#:     ]
STEREO_CODEC = "aac"
STEREO_BITRATE = "160k"

#: The surround track: passed through when the container and the copy mask
#: allow it, re-encoded otherwise. Same preset, "AudioCopyMask": ["copy:aac",
#: "copy:ac3"] — deliberately narrower than everything MP4 can hold, because
#: those two are what plays everywhere.
PASSTHROUGH_AUDIO = frozenset({"aac", "ac3"})
SURROUND_BITRATE = "640k"

#: And its fallback, "AudioEncoderFallback": "av_aac". Not AC-3: the preset
#: asks for AAC, which keeps the channel count without the 640k ceiling.
SURROUND_FALLBACK = "aac"


def describe_audio_choice(streams: list[dict], wanted: str) -> str:
    """One line saying which audio track will lead, and why.

    Written for the job log, and specifically for the question "why is this
    still in English". That has three causes which look identical from the
    outside: nothing was asked for, the disc carries no language tags at all,
    or the language asked for is not on this disc. They need three different
    things done about them — set the language, accept that this disc cannot be
    matched, or check what the disc actually holds — so the line says which.
    """
    if not streams:
        return "Audio: nothing could be read about the tracks on this file."

    listing = ", ".join(
        f"{index}:{stream.get('language') or 'untagged'} ({stream['codec']})"
        for index, stream in enumerate(streams)
    )

    if not wanted:
        return (
            f"Audio: {len(streams)} track(s) — {listing}. No spoken language is "
            "set, so the disc's own order is kept and track 0 leads. "
            "Settings → Encoding → Spoken language changes that."
        )

    tagged = [s for s in streams if s.get("language")]
    if not tagged:
        return (
            f"Audio: {len(streams)} track(s) — {listing}. None of them carries a "
            f"language tag, so '{wanted}' cannot be matched against anything and "
            "track 0 leads. That is how the disc was authored, not a setting."
        )

    chosen = preferred_track(streams, wanted)
    if language_matches(streams[chosen].get("language", ""), wanted):
        return (
            f"Audio: {len(streams)} track(s) — {listing}. Track {chosen} matches "
            f"'{wanted}' and leads; it is kept as a stereo downmix and as the "
            "surround track."
        )
    return (
        f"Audio: {len(streams)} track(s) — {listing}. None is '{wanted}', so the "
        "disc's own order is kept and track 0 leads."
    )


def audio_went_missing(
    exe: str, source: Path, produced: Path, source_streams: list[dict] | None = None,
) -> str:
    """Why the encode should be treated as failed, or "" if the audio survived.

    An encoder can drop every audio track and still exit 0. HandBrake does it
    whenever the language list matches nothing on the disc, and twice more
    besides — an Auto Passthru it cannot satisfy, a mixdown at a samplerate it
    will not take. The result is a film that plays perfectly, in silence,
    reported as a success; the only thing that notices is somebody sitting
    down to watch it a week later, with the raw files cleaned up and the disc
    back on the shelf.

    So the output is asked what it ended up with, and the source is asked too,
    because neither fact means anything on its own: a disc that never had
    sound is not a fault, and refusing to finish one would be a new bug
    replacing the old one.

    **Both answers have to be positive before this accuses anything.**
    ``audio_streams`` returns an empty list for "no audio" and for "could not
    look" alike — no ffprobe, a timeout on a stalled mount, a container it
    does not know — so an empty answer about the *output* is not evidence that
    anything was lost. The duration settles it: a file that reports one was
    read successfully, and only then does an empty audio list mean what it
    says. Without that this failed perfectly good encodes whenever the output
    was the thing it could not read, and an output written straight to a NAS
    is not a rare shape.

    *source_streams* lets a caller that has already probed the source pass the
    answer in rather than paying for it twice.
    """
    if not source.exists() or not produced.exists():
        return ""
    streams = audio_streams(exe, source) if source_streams is None else source_streams
    if not streams:
        return ""
    if audio_streams(exe, produced):
        return ""
    if not probe_duration(exe, produced):
        logger.debug("Could not read %s, so its audio is unknown", produced)
        return ""
    return (
        f"The encode finished with no audio at all: {source.name} has sound "
        f"and {produced.name} has none. Nothing that plays silently is worth "
        "keeping, so this counts as a failure rather than a finished film. "
        "Retry re-encodes it from the raw files — the disc is not needed."
    )


def audio_plan(
    exe: str, input_path: Path, output_path: Path, language: str = "",
) -> list[str]:
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

    streams = audio_streams(exe, input_path)
    if not streams:
        # Nothing could be read about the audio. AC-3 for everything is the
        # safe guess: it costs quality on a track that might have copied
        # fine, where the other way round costs the entire encode.
        return ["-map", "0:a?", *FALLBACK_AUDIO]

    if language:
        return _preset_shaped_plan(streams, language)
    return _keep_everything_plan(streams)


def _preset_shaped_plan(streams: list[dict], language: str) -> list[str]:
    """One language, two tracks — the shape the HandBrake preset specifies.

    ``AudioLanguageList: ["swe"]`` with ``AudioTrackSelectionBehavior:
    "first"`` and two entries in ``AudioList``: the chosen track becomes an
    AAC stereo downmix *and* a surround track, and the other languages on the
    disc are not carried.

    That last part was a deliberate choice in the preset and this used to
    override it, keeping every language "to be generous". Generous is not the
    same as correct: someone who asks for Swedish has said what they want out.
    """
    chosen = preferred_track(streams, language)
    codec = streams[chosen]["codec"]
    tag = streams[chosen].get("language", "")

    # The same source track twice: once downmixed, once as it came.
    args = ["-map", f"0:a:{chosen}", "-map", f"0:a:{chosen}"]
    args += ["-c:a:0", STEREO_CODEC, "-ac:a:0", "2", "-b:a:0", STEREO_BITRATE]
    if codec in PASSTHROUGH_AUDIO:
        args += ["-c:a:1", "copy"]
    else:
        args += ["-c:a:1", SURROUND_FALLBACK, "-b:a:1", SURROUND_BITRATE]

    # Both are the same speech and should say so. The downmix especially: it
    # is a new stream carrying none of the original's tags, so a player would
    # list it as "Undetermined" and Plex would file it as such.
    if tag:
        args += ["-metadata:s:a:0", f"language={tag}",
                 "-metadata:s:a:1", f"language={tag}"]

    # The stereo track leads. Flagging the surround one as well would let a
    # player choose the AC-3 — the track some hardware cannot decode from an
    # MP4, and the silent film this arrangement exists to prevent.
    #
    # And the surround one is explicitly cleared, because ffmpeg copies the
    # source's dispositions: the disc's own default flag came along with the
    # track, so both were marked default and a player was free to pick either.
    # Setting one without clearing the other was a no-op on exactly the discs
    # this matters for.
    args += ["-disposition:a:0", "default", "-disposition:a:1", "0"]
    return args


def _keep_everything_plan(streams: list[dict]) -> list[str]:
    """No language asked for, so nothing is thrown away.

    Choosing a track for someone who has not said which language they want
    would be guessing at the one thing they care most about. Every source
    track comes across, with a stereo downmix of the first in front so
    something plays on hardware that cannot decode the rest.
    """
    args = ["-map", "0:a:0", "-map", "0:a?"]
    args += ["-c:a:0", STEREO_CODEC, "-ac:a:0", "2", "-b:a:0", STEREO_BITRATE]

    tag = streams[0].get("language", "")
    if tag:
        args += ["-metadata:s:a:0", f"language={tag}"]

    for index, stream in enumerate(streams):
        out = index + 1
        if stream["codec"] in MP4_AUDIO:
            args += [f"-c:a:{out}", "copy"]
        else:
            args += [f"-c:a:{out}", SURROUND_FALLBACK, f"-b:a:{out}", SURROUND_BITRATE]

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
    # The shared settings, so that switching encoders does not silently
    # change the result. 0 means "nothing was asked for", and here that means
    # this encoder's own default rather than a preset's.
    from adr.encodingsettings import clamp_quality

    quality = clamp_quality(getattr(config, "video_quality", 0)) or DEFAULT_QUALITY
    try:
        height = max(0, int(getattr(config, "max_height", 0) or 0))
    except (TypeError, ValueError):
        height = 0

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
        # The setting first, then the HandBrake preset behind it. Someone who
        # set Swedish in the preset and switched to the GPU encoder because
        # HandBrake could not use the GPU meant Swedish either way; reading
        # only the setting is how that turned back into English.
        from adr.encodingsettings import language as wanted_language

        wanted = wanted_language(self._config)
        streams = audio_streams(self._exe, input_path)

        # Say which track was chosen and why, in the job's own log.
        #
        # "still in English" has three completely different causes — nothing
        # was asked for, the disc tags no languages so nothing can be matched,
        # or the language asked for is not on this disc — and from the outside
        # they look identical. Each needs a different thing done about it, so
        # the log says which one it was rather than leaving it to be guessed.
        if self.log_sink:
            self.log_sink(describe_audio_choice(streams, wanted))

        cmd = build_command(
            self._exe, input_path, output_path, self._config,
            audio_plan(self._exe, input_path, output_path, wanted),
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
            # A clean exit is not proof there is anything to listen to. The
            # mapping here always keeps a track, but a codec that refused
            # halfway would still leave ffmpeg happy and the film mute, and
            # that is not a thing to find out a week later.
            silent = audio_went_missing(
                self._exe, input_path, output_path, streams,
            )
            if silent:
                result.error = silent
                logger.error("Encode produced no audio: %s", output_path)
                if self.log_sink:
                    self.log_sink(silent)
                return result
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
