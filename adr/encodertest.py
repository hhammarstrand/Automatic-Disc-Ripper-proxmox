"""Ask HandBrake whether it can encode with the settings it has been given.

A preset that HandBrake cannot satisfy fails identically on every title of
every disc: exit code 3, an initialisation error, forty minutes after the disc
went in and once per title. Ten titles, ten identical failures, and the cause
is a line of stderr nobody sees.

The cause is always one of a small set — an encoder the build does not have, a
hardware encoder that is not present in a container, an audio codec missing,
or a preset name that does not resolve — and every one of them can be found in
two seconds without a disc. HandBrake will tell you, if asked.

Two probes, cheapest first:

1. **Does the preset resolve?** ``--preset-import-file`` plus a scan of a file
   that does not exist. HandBrake loads presets before it opens anything, so a
   bad preset name fails here and a good one gets as far as complaining about
   the missing input — which is the answer we wanted.

2. **Can it actually encode with it?** Two seconds of generated video, made
   with ffmpeg and run through the real preset. This is the one that catches
   an encoder the build was compiled without, because nothing before it
   touches the codec.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: Loading a preset is instant; a stuck HandBrake here is a broken install.
PRESET_TIMEOUT = 30

#: Two seconds of video. Generous enough for a slow container, short enough
#: that nobody minds pressing the button.
ENCODE_TIMEOUT = 120

#: Lines that are noise on the way past.
_NOISE = ("hb_display", "Compile-time hardening", "libhb: ")

#: What HandBrake's own words mean, in the order a human would check them.
#: Matching its text rather than the exit code because the code is always 3.
_EXPLANATIONS = (
    (r"preset.*not found|invalid preset|unknown preset",
     "The preset name does not exist in the file that was imported. "
     "Settings → HandBrake preset must match a name inside the preset file."),
    # Hardware is handled separately, by _hardware_advice: whether to pass the
    # GPU through or change the preset depends on what the container has, and
    # one sentence for both throws away a working answer.
    (r"unknown (video |audio )?encoder|encoder .* not (found|supported)|no such encoder",
     "This build of HandBrake was compiled without the encoder the preset "
     "asks for. Pick a preset that uses x264, which every build has."),
    (r"audio", "HandBrake could not set up the audio the preset asks for — a "
               "surround or passthrough track this build cannot produce."),
)


def _step(name: str, status: str, detail: str, fix: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def _explain(output: str) -> str:
    """Turn HandBrake's complaint into something to do about it."""
    from adr import gpu

    if gpu.mentions_hardware(output):
        return _hardware_advice()
    lowered = output.lower()
    for pattern, advice in _EXPLANATIONS:
        if re.search(pattern, lowered):
            return advice
    return ""


def _hardware_advice() -> str:
    """What to do when the preset wants a GPU.

    Two different answers, and which one is right depends on whether the
    container has a render node. Telling someone to "use a software preset"
    when the hardware is one config line away throws away the reason they
    chose that preset; telling them to pass a GPU through when the host has
    none wastes their evening.
    """
    from adr import gpu

    state = gpu.describe()
    if state["available"]:
        return (
            "The preset asks for a hardware encoder and this container does "
            f"have {state['nodes'][0]} — so the encoder itself is missing from "
            "this HandBrake build rather than the GPU being absent. A software "
            "preset (x264 or x265) will work."
        )
    return (
        "The preset asks for a hardware encoder, and this container has no GPU. "
        + state["detail"]
        + " Either pass the GPU through — " + (state["fix"] or "adr-doctor --fix {ctid}")
        + " — or pick a software preset such as 'Fast 1080p30' under "
        "Settings → HandBrake preset. Software is slower but needs nothing "
        "from the host."
    )


def _meaningful(output: str, limit: int = 6) -> str:
    """The lines of HandBrake's output worth showing a person."""
    lines = [
        line.strip() for line in output.splitlines()
        if line.strip() and not any(n in line for n in _NOISE)
    ]
    errors = [line for line in lines if re.search(r"error|fail|invalid|unknown|no such", line, re.I)]
    chosen = errors or lines
    return "\n".join(chosen[-limit:])


def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    """Run HandBrake and return ``(exit code, combined output)``."""
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
        )
    except FileNotFoundError:
        return -1, f"{cmd[0]} is not installed."
    except subprocess.TimeoutExpired:
        return -1, f"HandBrake did not answer within {timeout}s."
    except OSError as exc:
        return -1, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def test_encoder(config) -> dict:
    """Run the probes. Returns ``{"ok", "summary", "steps": [...]}``."""
    steps: list[dict] = []
    exe = config.handbrake_path

    if not (shutil.which(exe) or os.path.isfile(exe)):
        steps.append(_step(
            "HandBrake", "fail", f"{exe} is not installed.",
            "pct exec {ctid} -- /opt/adr/scripts/update.sh",
        ))
        return _finish(steps)
    steps.append(_step("HandBrake", "ok", f"{exe} is present."))

    preset_file = _preset_file(config)
    steps.append(_preset_step(exe, preset_file, config.handbrake_preset))
    if steps[-1]["status"] == "fail":
        return _finish(steps)

    steps.append(_encode_step(exe, preset_file, config))
    return _finish(steps)


def _preset_file(config) -> str:
    """The preset file the encoder would actually import."""
    from adr.encoder import HandBrakeEncoder

    configured = getattr(config, "handbrake_preset_file", "") or ""
    if configured:
        return configured
    probe = HandBrakeEncoder.__new__(HandBrakeEncoder)
    probe._preset = config.handbrake_preset
    return probe._auto_discover_preset_file()


def _preset_step(exe: str, preset_file: str, preset_name: str) -> dict:
    """Does the preset name resolve at all?"""
    cmd = [exe]
    if preset_file and os.path.isfile(preset_file):
        cmd += ["--preset-import-file", preset_file]
    cmd += [f"--preset={preset_name}", "--preset-list"]

    code, output = _run(cmd, PRESET_TIMEOUT)
    if code == -1:
        return _step("Preset", "fail", output)
    if preset_name and preset_name not in output:
        return _step(
            "Preset", "warn",
            f"'{preset_name}' did not appear in HandBrake's preset list. "
            + (f"Imported: {preset_file}. " if preset_file else "")
            + "It may still resolve as a built-in.",
            "Settings → HandBrake preset",
        )
    where = f" (from {preset_file})" if preset_file else " (built-in)"
    return _step("Preset", "ok", f"HandBrake knows '{preset_name}'{where}.")


def _encode_step(exe: str, preset_file: str, config) -> dict:
    """Encode two seconds of generated video with the real preset.

    This is the probe that matters. Everything before it can pass on a build
    with no usable video encoder, because nothing before it touches one.

    The sample is made with ffmpeg, which is already a dependency for audio
    CDs. HandBrake has no test-pattern generator of its own that can be relied
    on across versions, and guessing at a flag that may not exist would turn a
    working setup into a red cross.
    """
    ffmpeg = getattr(config, "ffmpeg_path", "") or "ffmpeg"
    if not (shutil.which(ffmpeg) or os.path.isfile(ffmpeg)):
        return _step(
            "Test encode", "warn",
            "ffmpeg is not installed, so a sample could not be made and the "
            "encode itself could not be tried without a disc.",
            "pct exec {ctid} -- apt-get install -y ffmpeg",
        )

    workdir = Path(tempfile.mkdtemp(prefix="adr-encodertest-"))
    try:
        sample = workdir / "sample.mkv"
        code, text = _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=640x360:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(sample),
        ], ENCODE_TIMEOUT)
        if code != 0 or not sample.exists():
            # ffmpeg's problem, not HandBrake's — say so plainly rather than
            # printing a bare exit code, which reads as an encoder failure.
            why = _meaningful(text) or (
                f"it exited {code}" if code else "it wrote no file"
            )
            return _step(
                "Test encode", "warn",
                f"ffmpeg could not make a sample to encode, so HandBrake was "
                f"not tried: {why}. This says nothing about your preset.",
            )

        output = workdir / "probe.mp4"
        cmd = [exe, "-i", str(sample), "-o", str(output)]
        if preset_file and os.path.isfile(preset_file):
            cmd += ["--preset-import-file", preset_file]
        cmd.append(f"--preset={config.handbrake_preset}")
        extra = getattr(config, "handbrake_extra_args", "") or ""
        if extra:
            cmd += extra.split()

        code, text = _run(cmd, ENCODE_TIMEOUT)
        if code == 0 and output.exists() and output.stat().st_size > 0:
            return _step(
                "Test encode", "ok",
                "HandBrake encoded a two-second sample with this preset. "
                "The encoder and the preset are both fine.",
            )
        said = _meaningful(text)
        return _step(
            "Test encode", "fail",
            f"HandBrake could not encode with this preset (exit {code}).\n{said}",
            _explain(text)
            or "Try a built-in preset such as 'Fast 1080p30' under Settings.",
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _finish(steps: list[dict]) -> dict:
    failed = [s for s in steps if s["status"] == "fail"]
    if failed:
        summary = failed[0]["detail"].splitlines()[0]
    elif any(s["status"] == "warn" for s in steps):
        summary = "HandBrake works, with one thing worth reading."
    else:
        summary = "HandBrake encoded a sample with your settings. Encoding works."
    return {"ok": not failed, "summary": summary, "steps": steps}


def with_ctid(result: dict, ctid: str | None) -> dict:
    """Fill the container id into any command the result suggests."""
    for step in result["steps"]:
        if step.get("fix"):
            step["fix"] = step["fix"].replace("{ctid}", ctid or "<CTID>")
    return result


# ------------------------------------------------------------------ #
# The way out that needs nothing from the Proxmox host
#
# Passing a GPU through means editing the container's config, which the
# container deliberately cannot do. Changing the preset needs nothing but this
# page — so when the encoder is the problem, that is the fix worth offering,
# and offering it as a button rather than a sentence about where to go.
# ------------------------------------------------------------------ #

#: Built-in presets whose names give away a hardware encoder.
_HARDWARE_IN_NAME = ("qsv", "nvenc", "vce", "vaapi", "videotoolbox", "mf ")


def _builtin_presets(exe: str) -> list[str]:
    """Every preset HandBrake knows without importing a file."""
    code, output = _run([exe, "--preset-list"], PRESET_TIMEOUT)
    if code == -1:
        return []
    names = []
    for line in output.splitlines():
        stripped = line.strip()
        # HandBrake indents preset names under their category heading and
        # follows each with a wrapped description; the names are the lines
        # that are indented but not deeply, and carry no sentence punctuation.
        if not stripped or stripped.endswith("/") or stripped.endswith("."):
            continue
        indent = len(line) - len(line.lstrip())
        if 2 <= indent <= 8 and len(stripped) < 60:
            names.append(stripped)
    return names


def software_alternatives(config) -> list[str]:
    """Software presets that could replace the current one, closest first.

    Ordered by resemblance to the name already configured, because someone who
    chose "Super HQ 1080p30 Surround (Svenska)" wants that quality, not the
    first entry in an alphabetical list. The stock "Super HQ 1080p30 Surround"
    is the same preset without the hardware encoder, and that is the answer.
    """
    import difflib

    names = [
        name for name in _builtin_presets(config.handbrake_path)
        if not any(marker in name.lower() for marker in _HARDWARE_IN_NAME)
    ]
    if not names:
        return []

    current = (config.handbrake_preset or "").lower()
    # Strip a parenthesised suffix: a localised copy is still the same preset.
    base = re.sub(r"\s*\([^)]*\)\s*$", "", current).strip()

    # Resemblance orders the list; it must not shorten it. Every software
    # preset HandBrake has is a valid choice, and dropping the ones that
    # happen not to resemble the broken name would hide the good ones.
    def similarity(name: str) -> float:
        return difflib.SequenceMatcher(None, base, name.lower()).ratio()

    return sorted(names, key=similarity, reverse=True)
