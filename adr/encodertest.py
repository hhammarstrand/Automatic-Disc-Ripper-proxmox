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

#: The prefix every hardware encoder name carries, before the underscore:
#: qsv_h264, nvenc_h265, vce_av1, vaapi_h264, vt_h264, mf_h264.
_HARDWARE_FAMILIES = frozenset({"qsv", "nvenc", "vce", "vaapi", "vt", "mf"})

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


def _explain(output: str, exe: str = "", hardware: dict | None = None) -> str:
    """Turn HandBrake's complaint into something to do about it."""
    from adr import gpu

    if gpu.mentions_hardware(output):
        return _hardware_advice(exe, hardware)
    lowered = output.lower()
    for pattern, advice in _EXPLANATIONS:
        if re.search(pattern, lowered):
            return advice
    return ""


def build_hardware_encoders(exe: str) -> list[str]:
    """The hardware encoders HandBrake reports as available right now.

    Worth being precise about, because reading this list as "what the build
    was compiled with" cost a round of wrong advice. HandBrake filters
    ``--help`` by what it can actually start on this machine, so a build whose
    Quick Sync runtime fails to initialise lists no hardware encoder at all —
    indistinguishable, here, from a build that never had one. The evidence
    that tells those apart is an encode, not a list; see ``_try_encoder``.
    """
    code, output = _run([exe, "--help"], PRESET_TIMEOUT)
    if code == -1:
        return []
    # Matched as whole encoder names — family, underscore, codec — rather than
    # by looking for "qsv" anywhere. --help also documents --qsv-async-depth
    # and --enable-qsv-decoding, and a substring search would read those as an
    # encoder and hand back the wrong one of three answers.
    return sorted({
        name for name in re.findall(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b", output.lower())
        if name.split("_", 1)[0] in _HARDWARE_FAMILIES
    })


def _hardware_advice(exe: str = "", hardware: dict | None = None) -> str:
    """What to do when the preset wants a GPU.

    Several situations produce the same HandBrake error and have completely
    different fixes: no render node, a node with no driver stack, a driver
    stack whose Quick Sync runtime will not start, or a GPU that genuinely
    cannot encode. Telling someone to "use a software preset" when the
    hardware is one line away throws away the reason they chose that preset;
    telling them to pass a GPU through that is already there wastes an
    evening.

    The answer comes from what was *tried*, not from what was listed. An
    earlier version read ``--help``, found no hardware encoder, and concluded
    the build had none compiled in — but that list holds the encoders
    HandBrake can start *right now*, so a build whose Quick Sync runtime fails
    to initialise lists none either. That produced a confident "give up the
    hardware" on a machine whose GPU was working perfectly well.
    """
    from adr import gpu

    state = gpu.describe()
    working = (hardware or {}).get("working") or []

    if working:
        best = working[0]
        if best["driver"]:
            # The answer nobody could have guessed: the GPU, the Media SDK and
            # HandBrake were all fine, and libva was loading the wrong driver.
            return (
                f"HandBrake *can* use this GPU — {best['encoder']} encoded the "
                f"test sample once LIBVA_DRIVER_NAME was set to "
                f"{best['driver']}. Without it libva loads a different VA "
                "driver, and Quick Sync reports the hardware as absent. "
                "'Use HandBrake with the GPU' below sets it and re-tests."
            )
        return (
            "The preset's encoder will not start, but this HandBrake can use "
            f"the GPU: {', '.join(w['encoder'] for w in working)} encoded the "
            "test sample. Change VideoEncoder in your preset file to one of "
            "those, or use 'Use HandBrake with the GPU' below."
        )

    # The GPU is not lost just because HandBrake cannot reach it. ffmpeg goes
    # through VA-API rather than the Media SDK, and where that works this is
    # the answer someone actually wants — the hardware they have, at the speed
    # they expected, instead of an hour per film on the CPU.
    elsewhere = (hardware or {}).get("ffmpeg_gpu") or {}
    if elsewhere.get("ok"):
        return (
            "HandBrake cannot reach this GPU — its Quick Sync path goes "
            "through the Intel Media SDK, which is deprecated and no longer "
            "starts on current drivers. ffmpeg reaches the same hardware "
            "through VA-API and encoded a test clip here ("
            + ", ".join(elsewhere.get("codecs") or []) + "). "
            "'Encode on the GPU with ffmpeg' below switches to it and keeps "
            "your hardware speed."
        )

    if hardware is not None and state["available"] and state["runtime"]["ok"]:
        probe = gpu.vainfo()
        if probe["ran"] and probe["ok"]:
            # Everything checks out and nothing encodes. Worth saying plainly
            # rather than blaming a component that has just been shown to
            # work — an honest dead end beats a confident wrong cause.
            return (
                "The GPU works — vainfo loads the driver and lists encode "
                "profiles — but no hardware encoder in this HandBrake would "
                "start, including the one the preset asks for. That points at "
                "the build's Quick Sync runtime rather than at your hardware "
                "or your container. Encode in software with the button below; "
                "it is slower and it works today."
            )

    if state["available"] and not state["runtime"]["ok"]:
        # The case that looks solved and is not: the node is passed through,
        # so every check about the GPU passes, and the encode still fails
        # because nothing above the kernel is installed. Do not send someone
        # who has just finished passing a GPU through back to software.
        runtime = state["runtime"]
        return (
            f"The preset asks for a hardware encoder and {state['nodes'][0]} is "
            f"passed through correctly — but {runtime['detail']} That is why "
            "HandBrake says the hardware is not available. "
            + (runtime["fix"] or state["fix"] or "adr-doctor --fix {ctid}")
            + " installs it. Until then, 'Encode in software instead' below "
            "will get the disc finished."
        )
    if state["available"]:
        return (
            "The preset asks for a hardware encoder, this container has "
            f"{state['nodes'][0]}, and the driver for it is installed — so what "
            "is left is HandBrake's own hardware support failing to start. A "
            "software preset (x264 or x265) will work."
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


def _run(cmd: list[str], timeout: int, env: dict | None = None) -> tuple[int, str]:
    """Run HandBrake and return ``(exit code, combined output)``."""
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
            env=env,
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

    # Test what will actually run. Reporting a red cross about a HandBrake
    # preset that nothing is going to use — because encoding moved to the GPU
    # — is the same class of wrong answer this whole page exists to prevent.
    if getattr(config, "encoder_backend", "handbrake") == "vaapi":
        return _finish(_vaapi_steps(config))

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

    # One sample, shared. Both remaining steps encode something, and making
    # it twice would double the wait for no extra information.
    workdir = Path(tempfile.mkdtemp(prefix="adr-encodertest-"))
    try:
        sample, sample_problem = _make_sample(config, workdir)

        hardware = _hardware_step(exe, preset_file, config, sample, workdir)
        if hardware:
            steps.append(hardware)

        steps.append(
            _encode_step(exe, preset_file, config, sample, sample_problem, hardware),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return _finish(steps)


def _vaapi_steps(config) -> list[dict]:
    """The same question, asked of the GPU: will it encode with these settings?

    Deliberately the real encoder rather than a hand-built ffmpeg line. A test
    that passes while the thing it stands in for fails is worse than no test,
    and the only way to be sure they agree is for them to be the same code.
    """
    from adr import vaapi
    from adr.encoderfactory import describe_backend

    steps = [_step("Encoder", "ok", describe_backend(config))]

    ffmpeg = getattr(config, "ffmpeg_path", "") or "ffmpeg"
    if not (shutil.which(ffmpeg) or os.path.isfile(ffmpeg)):
        steps.append(_step(
            "ffmpeg", "fail", f"{ffmpeg} is not installed.",
            "pct exec {ctid} -- apt-get install -y ffmpeg",
        ))
        return steps

    state = vaapi.probe(config)
    steps.append(_step(
        "GPU", "ok" if state["ok"] else "fail", state["detail"],
        "" if state["ok"] else
        "Settings → Encoding → Encoder, or run adr-doctor --fix {ctid} on the host",
    ))
    if not state["ok"]:
        return steps

    workdir = Path(tempfile.mkdtemp(prefix="adr-encodertest-"))
    try:
        sample, problem = _make_sample(config, workdir)
        if sample is None:
            steps.append(_step("Test encode", "warn", problem))
            return steps

        # The encoder the pipeline would use, on a real file, writing a real
        # output — including the audio decision, which is the part that fails
        # at the very end of a two-hour encode rather than at the start.
        encoder = vaapi.VaapiEncoder(config)
        said: list[str] = []
        encoder.log_sink = said.append
        result = encoder.encode(sample, output_dir=workdir / "out")
        if result.success:
            steps.append(_step(
                "Test encode", "ok",
                "ffmpeg encoded a two-second sample on the GPU with these "
                "settings, audio and all. Ripping will work.",
            ))
        else:
            steps.append(_step(
                "Test encode", "fail", result.error or "The encode failed.",
                "Settings → Encoding, or 'Encode in software instead'",
            ))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return steps


def _make_sample(config, workdir: Path) -> tuple[Path | None, str]:
    """Two seconds of generated video to encode. ``(path, why not)``.

    ffmpeg is already a dependency for audio CDs. HandBrake has no test
    pattern generator that can be relied on across versions, and guessing at
    a flag that may not exist would turn a working setup into a red cross.
    """
    ffmpeg = getattr(config, "ffmpeg_path", "") or "ffmpeg"
    if not (shutil.which(ffmpeg) or os.path.isfile(ffmpeg)):
        return None, (
            "ffmpeg is not installed, so a sample could not be made and the "
            "encode itself could not be tried without a disc."
        )

    sample = workdir / "sample.mkv"
    code, text = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=duration=2:size=640x360:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:v", "libx264", "-c:a", "aac", "-shortest",
        # Tagged with the language that is configured, so a language filter
        # is actually exercised instead of quietly matching nothing.
        "-metadata:s:a:0",
        f"language={_wanted_language(config) or 'und'}",
        str(sample),
    ], ENCODE_TIMEOUT)
    if code != 0 or not sample.exists():
        why = _meaningful(text) or (f"it exited {code}" if code else "it wrote no file")
        return None, (
            f"ffmpeg could not make a sample to encode, so HandBrake was not "
            f"tried: {why}. This says nothing about your preset."
        )
    return sample, ""


def _wanted_language(config) -> str:
    """The language a real encode would ask for.

    Both encoders go through encodingsettings.language(), which falls back to
    the HandBrake preset when the setting is blank. Reading only the setting
    tagged the sample "und" on every install that had chosen its language in
    the preset — so the probe exercised the "no language wanted" path while a
    real encode took the other one.
    """
    from adr.encodingsettings import language

    return language(config)


def _try_encoder(
    exe: str, sample: Path, encoder: str, workdir: Path, driver: str | None = None,
) -> bool:
    """Can HandBrake actually encode with *encoder*, right now, on this box?

    *driver* is the value for ``LIBVA_DRIVER_NAME``, or None to leave libva to
    its own choice. It is a parameter rather than a constant because which one
    works is not knowable in advance — it depends on what the Media SDK in
    this container was built against, which nothing reports.
    """
    output = workdir / f"probe-{encoder}-{driver or 'default'}.mp4"
    env = None
    if driver:
        env = dict(os.environ)
        env["LIBVA_DRIVER_NAME"] = driver
    code, _ = _run([
        exe, "-i", str(sample), "-o", str(output), "-e", encoder,
    ], ENCODE_TIMEOUT, env=env)
    return code == 0 and output.exists() and output.stat().st_size > 0


def _describe_pairing(pairing: dict) -> str:
    """"qsv_h264 (with LIBVA_DRIVER_NAME=i965)" — the driver matters and is
    invisible, so it is spelled out rather than silently applied."""
    if pairing["driver"]:
        return f"{pairing['encoder']} (with LIBVA_DRIVER_NAME={pairing['driver']})"
    return pairing["encoder"]


def _hardware_step(
    exe: str, preset_file: str, config, sample: Path | None, workdir: Path,
) -> dict | None:
    """What the hardware stack actually does, for a preset that needs one.

    Only shown when the preset asks for a GPU. For the majority who encode in
    software this is a paragraph about hardware they never wanted, and a page
    that reports things nobody asked about trains people to stop reading it.

    The last part is a live probe rather than a reading of ``--help``, and
    that distinction cost a round of wrong advice. HandBrake's ``--help``
    lists the encoders available *right now*, not the ones the build was
    compiled with, so a build whose Quick Sync cannot initialise lists none —
    and reading that as "not compiled in" produced a confident, wrong "give up
    the hardware". Trying each candidate on two seconds of video answers the
    question that actually matters: what will encode on this machine today.

    It never fails the run on its own. The encode below is the verdict.
    """
    from adr import gpu

    wanted = gpu.preset_wants_hardware(preset_file, config.handbrake_preset)
    if not wanted:
        return None

    lines = [f"The preset '{config.handbrake_preset}' encodes with '{wanted}'."]

    available = build_hardware_encoders(exe)
    lines.append(
        f"HandBrake reports these hardware encoders as available: "
        f"{', '.join(available)}." if available else
        "HandBrake reports no hardware encoder as available. That is a "
        "runtime answer, not a compile-time one — the list only holds "
        "encoders it can start on this machine."
    )

    state = gpu.describe()
    lines.append(state["detail"])

    probe = gpu.vainfo()
    if probe["ran"] and probe["ok"]:
        lines.append(
            f"vainfo: the driver loads ({probe['driver'] or 'unnamed'}) and "
            f"offers {len(probe['encoders'])} encode profile(s), so the GPU "
            "itself can encode."
        )
    elif probe["ran"]:
        lines.append(
            "vainfo ran but the stack offers no encode profile at all, so "
            "nothing here can encode in hardware whatever the preset says. "
            "Its answer: " + (_meaningful(probe["output"], limit=3) or "(none)")
        )
    elif probe["output"]:
        lines.append(probe["output"])

    working = _working_hardware_encoders(exe, sample, workdir, wanted)
    if sample is None:
        lines.append("No sample could be made, so no encoder was actually tried.")
    elif working:
        lines.append(
            "Tried on two seconds of video, HandBrake encoded with: "
            + ", ".join(_describe_pairing(w) for w in working) + "."
        )
    else:
        lines.append(
            "Tried on two seconds of video, no hardware encoder in HandBrake "
            "would start."
        )

    # HandBrake failing to reach the GPU says nothing about whether the GPU
    # can be reached. ffmpeg goes through VA-API instead of the Media SDK, and
    # on an Intel container that is very often the difference between hardware
    # encoding and an hour per film on the CPU.
    if not working:
        from adr import vaapi

        elsewhere = vaapi.probe(config)
        if elsewhere["ok"]:
            lines.append(
                "ffmpeg, however, did encode on this GPU ("
                + ", ".join(elsewhere["codecs"]) + "). The hardware is fine; it "
                "is HandBrake that cannot reach it."
            )
        step_extra = elsewhere
    else:
        step_extra = {"ok": False, "codecs": [], "detail": ""}

    ok = bool(working)
    step = _step(
        "Hardware", "ok" if ok else "warn", "\n".join(lines),
        "" if ok else (state["runtime"]["fix"] or state["fix"]),
    )
    step["working"] = working
    step["ffmpeg_gpu"] = step_extra
    return step


def _working_hardware_encoders(
    exe: str, sample: Path | None, workdir: Path, wanted: str,
) -> list[dict]:
    """Which hardware encoder and VA driver pairing actually encodes.

    Two things vary and neither can be read off anything: which encoder this
    build can start, and which VA-API driver its Quick Sync was built against.
    A container with both ``iHD`` and ``i965`` installed can have a working
    GPU and a working Media SDK that cannot see each other, because libva
    loaded the other one — and it reports that as "qsv is not available on the
    system", which is what it also says when there is no GPU at all.

    So every pairing is tried. Each is a real two-second encode, and the loop
    stops at the first success per encoder, because the answer is "one that
    works", not "all of them".

    Returns ``[{"encoder", "driver"}]``, driver being "" for libva's default.
    """
    from adr import gpu

    if sample is None:
        return []

    encoders = [wanted] if wanted else []
    for alternative in ("qsv_h265", "qsv_h264"):
        if alternative not in encoders:
            encoders.append(alternative)

    working = []
    for encoder in encoders:
        for driver in gpu.LIBVA_DRIVER_CANDIDATES:
            if _try_encoder(exe, sample, encoder, workdir, driver):
                working.append({"encoder": encoder, "driver": driver or ""})
                break
    return working


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


def _encode_step(
    exe: str, preset_file: str, config,
    sample: Path | None, sample_problem: str, hardware: dict | None = None,
) -> dict:
    """Encode two seconds of generated video with the real preset.

    This is the probe that matters. Everything before it can pass on a build
    with no usable video encoder, because nothing before it touches one.
    """
    if sample is None:
        # ffmpeg's problem, not HandBrake's — say so plainly rather than
        # printing a bare exit code, which reads as an encoder failure.
        return _step(
            "Test encode", "warn", sample_problem,
            "pct exec {ctid} -- apt-get install -y ffmpeg"
            if "not installed" in sample_problem else "",
        )

    output = sample.parent / "probe.mp4"
    cmd = [exe, "-i", str(sample), "-o", str(output)]
    if preset_file and os.path.isfile(preset_file):
        cmd += ["--preset-import-file", preset_file]
    cmd.append(f"--preset={config.handbrake_preset}")
    # The same overrides a real encode would carry. A test that skipped them
    # would pass on a flag HandBrake rejects, and the rejection would then
    # surface forty minutes into a rip instead of here.
    from adr.encodingsettings import handbrake_overrides

    # The sample goes in because the language flag depends on the file:
    # a source with no track in the wanted language is sent as 'any', and
    # that is the flag a real encode would carry. Testing without it
    # vouches for a command real encodes do not run.
    cmd += handbrake_overrides(config, sample)
    extra = getattr(config, "handbrake_extra_args", "") or ""
    if extra:
        cmd += extra.split()

    # The same environment a real encode gets.
    #
    # encoder.encode() runs HandBrake with LIBVA_DRIVER_NAME from the config;
    # this probe did not, so it exercised a different setup from the one it
    # exists to vouch for — and on the machines where the driver name is the
    # whole reason Quick Sync starts, the button that just pinned that name
    # then failed its own re-test and put every setting back.
    from adr.encoder import encode_env

    code, text = _run(cmd, ENCODE_TIMEOUT, env=encode_env(config))
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
        _explain(text, exe, hardware)
        or "Try a built-in preset such as 'Fast 1080p30' under Settings.",
    )


def _finish(steps: list[dict]) -> dict:
    failed = [s for s in steps if s["status"] == "fail"]
    if failed:
        summary = failed[0]["detail"].splitlines()[0]
    elif any(s["status"] == "warn" for s in steps):
        summary = "Encoding works, with one thing worth reading."
    else:
        # Names whichever encoder was actually tested, because a summary that
        # says "HandBrake" after the GPU passed reads as a stale result.
        who = steps[0]["detail"] if steps and steps[0]["name"] == "Encoder" else "HandBrake"
        summary = f"{who.rstrip('.')} encoded a sample with your settings. Encoding works."
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

#: Built-in presets whose names give away a hardware encoder. VCN is AMD's
#: current name for the engine it used to call VCE, and HandBrake ships
#: "H.265 VCN 1080p" presets on every platform whether or not the hardware is
#: there — so without it the software list offers a preset that fails exactly
#: like the one being escaped from.
_HARDWARE_IN_NAME = (
    "qsv", "nvenc", "vce", "vcn", "vaapi", "videotoolbox", "mf ",
)


def _builtin_presets(exe: str) -> list[str]:
    """Every preset HandBrake knows without importing a file.

    ``--preset-list`` is laid out by indentation::

        General/
            Very Fast 1080p30
                Small H.264 video (up to 1080p30) and AAC stereo audio,
                in an MP4 container.

    Categories sit at the left margin and end in a slash; names are one level
    in; descriptions are one level further and wrap across lines. Guessing at
    a range of indents catches the wrapped description lines too — which is
    how "and Dolby Digital (AC-3) surround audio, in an MP4" ended up being
    offered as something to encode with.

    So the name level is measured rather than assumed: it is the smallest
    indent any non-category line uses. That survives HandBrake changing its
    spacing, which a hardcoded 4 would not.
    """
    code, output = _run([exe, "--preset-list"], PRESET_TIMEOUT)
    if code == -1:
        return []

    rows = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.endswith("/"):
            continue                       # blank, or a category heading
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            continue                       # a heading that forgot its slash
        rows.append((indent, stripped))

    if not rows:
        return []
    name_indent = min(indent for indent, _ in rows)
    return [text for indent, text in rows if indent == name_indent]


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
