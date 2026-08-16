"""Everything needed to diagnose this installation, in one block of text.

Every diagnosis in this application's history has gone the same way: a
screenshot arrives, it shows a symptom, and the answer needs three other
things — the version, what the destination really is, what the tool actually
said. Each round trip costs a day.

This is that whole set, gathered in one place, so it can be copied once and
pasted anywhere. Which is also why it is plain text rather than JSON: it is
meant to be read by a person in a chat window, not parsed.

**Secrets are redacted here, not at the edges.** A bundle is made to be
pasted somewhere public, so nothing that could authenticate as the user may
leave in it — the TMDb key, the Plex token, the notification token, and the
notification URL, which for several providers *is* the credential. The
redaction is a whitelist of shape rather than a blacklist of names: a value is
shown only if its key is known to be harmless.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
import platform
import re
import sys
from datetime import datetime

logger = logging.getLogger(__name__)

#: Settings whose values are safe to show. Everything not named here is
#: reported as set-or-not and nothing more. A whitelist because the failure
#: modes are asymmetric: a missing setting costs a follow-up question, a
#: leaked token costs the user their account.
SAFE_KEYS = frozenset({
    "makemkv_path", "handbrake_path", "raw_path", "completed_path",
    "staging_path", "plex_path", "tv_path", "music_path", "data_disc_path",
    "watch_path", "watch_output_path", "watch_interval",
    "min_title_length", "handbrake_preset", "handbrake_preset_file",
    "handbrake_extra_args", "max_encode_jobs", "transcode_enabled",
    # Which encoder and how it is tuned. A device path and a quantiser
    # authenticate nothing, and redacting them makes the section about
    # hardware encoding unreadable in exactly the bundle someone pastes when
    # hardware encoding is what has gone wrong.
    "encoder_backend", "libva_driver",
    "audio_language", "video_quality", "max_height",
    "vaapi_device", "vaapi_codec",
    "drives", "disabled_drives", "no_eject_drives", "drive_labels",
    "eject_after_rip", "main_feature_only", "log_level",
    "require_completed_mount", "stage_locally", "auto_move_to_plex",
    "series_detection", "series_min_minutes", "series_max_minutes",
    "series_min_episodes", "series_mode", "series_mode_show",
    "series_mode_season", "series_mode_next_episode", "series_mode_discs",
    "series_mode_tmdb_id", "series_mode_year", "log_path",
    "skip_duplicates", "notify_enabled", "notify_provider", "notify_events",
    "plex_refresh_enabled", "plex_section",
    "audio_cd_enabled", "audio_cd_format", "audio_cd_mp3_bitrate",
    "cdparanoia_path", "ffmpeg_path", "data_disc_enabled",
    "web_host", "web_port",
})

#: Shortest configured value worth hunting for in free text. Below this a
#: setting is not a credential, and blanking every three-character string that
#: happens to appear somewhere would redact the bundle into uselessness.
MIN_SECRET_LENGTH = 8

#: Credentials carried in text nobody configured — a URL a library logged, a
#: header an error quoted. The name is matched, not the value, because the
#: value is exactly what is unknown here.
_SECRET_IN_TEXT = (
    re.compile(
        r"(?i)\b(api_?key|apikey|access_token|token|auth|password|passwd|pwd"
        r"|secret|signature)=([^&\s\"'<>]+)",
    ),
    re.compile(r"(?i)\b(x-plex-token)[=:]\s*([^&\s\"'<>]+)"),
    re.compile(r"(?i)^(\s*authorization:\s*\S+\s+)(\S+)", re.MULTILINE),
)

REDACTED = "<redacted>"


def scrub(text: str, config) -> str:
    """Remove anything that could authenticate, from anywhere in the bundle.

    Two passes, because there are two ways a secret gets in. The first is a
    configured one appearing in text that is not the settings section — the
    values are known, so they can be matched exactly. The second is a secret
    nobody configured here: a request URL logged by a library, a header quoted
    in a traceback. Those are matched by the *name* beside them, since the
    value is precisely what is not known.

    Neither pass is clever, and that is deliberate. This runs over a document
    written to be pasted into a public issue; a redaction that is easy to
    reason about is worth more than one that catches marginally more.
    """
    if not text:
        return text

    try:
        settings = config.as_dict()
    except Exception:                             # noqa: BLE001 - never fatal
        logger.warning("Could not read settings to scrub the bundle", exc_info=True)
        settings = {}

    # The values the application would actually use, not only the ones in the
    # file: a key supplied through /etc/default/adr never reaches adr.yaml, so
    # scrubbing by the file's contents alone had nothing to hunt for.
    with contextlib.suppress(Exception):
        settings = {**settings, **config.effective_secrets()}

    for key, value in settings.items():
        if key in SAFE_KEYS or not isinstance(value, str):
            continue
        if len(value.strip()) < MIN_SECRET_LENGTH:
            continue
        text = text.replace(value.strip(), REDACTED)

    for pattern in _SECRET_IN_TEXT:
        text = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    return text


#: How many recent failures to include, with their tool output.
FAILED_JOBS = 3

#: How much of each failed job's log to include. Enough for HandBrake's or
#: MakeMKV's parting words without pasting a whole encode.
JOB_LOG_LINES = 25

#: How much of the service log. The last few minutes of a failure.
SERVICE_LOG_LINES = 120


def build(config, pipeline_manager=None) -> str:
    """The whole report. Never raises — a broken section says so and the rest
    is still produced, because a partial bundle still answers most of it."""
    out: list[str] = []

    def section(title: str, body) -> None:
        out.append(f"\n=== {title} ===")
        try:
            text = body()
        except Exception as exc:                  # noqa: BLE001 - reported
            logger.exception("Diagnostics section %r failed", title)
            text = f"(this section could not be gathered: {exc})"
        out.append(text.rstrip() if text else "(nothing to report)")

    out.append("Automatic Disc Ripper — diagnostics")
    section("Version and host", lambda: _version())
    section("Will a rip work right now", lambda: _preflight(config, pipeline_manager))
    section("Self-checks", lambda: _checks(config))
    section("Storage", lambda: _storage(config))
    section("Optical drives", lambda: _drives())
    section("Hardware encoding", lambda: _hardware(config))
    section("Settings", lambda: _settings(config))
    section("Audio on the discs still in raw", lambda: _raw_audio(config))
    section("Auto-eject", lambda: _auto_eject(config))
    section("What the raw folder is holding", lambda: _raw_usage(config))
    section(f"Last {RECENT_JOBS} jobs, whatever their status", lambda: _recent_jobs(config))
    section(f"Last {FAILED_JOBS} failures", lambda: _failures(config))
    section(f"Service log (last {SERVICE_LOG_LINES} lines)", lambda: _service_log(config))
    # Last, over the whole thing, and not per section on purpose. The settings
    # section has been careful about secrets since it was written; the service
    # log was not, because nothing put a secret in it — until DEBUG turned on
    # urllib3, which logs every request URL in full, and a TMDb key rode out
    # in a bundle written to be pasted in public. Redacting section by section
    # is a rule someone has to remember at the moment they add a section. This
    # is the same rule applied where it cannot be forgotten.
    return scrub("\n".join(out) + "\n", config)


# ------------------------------------------------------------------ #
# Sections
# ------------------------------------------------------------------ #

def _version() -> str:
    from adr import __version__

    return "\n".join([
        f"ADR         {__version__}",
        f"Python      {sys.version.split()[0]}",
        f"Platform    {platform.platform()}",
        f"Generated   {datetime.now():%Y-%m-%d %H:%M:%S}",
    ])


def _preflight(config, pipeline_manager) -> str:
    import os

    from adr import preflight

    # with_ctid, as the dashboard does: a fix that still reads "{ctid}" is not
    # a command anyone can run, and the self-checks below already fill it in.
    result = preflight.with_ctid(
        preflight.check(config, pipeline_manager),
        os.environ.get("ADR_CTID", "").strip() or None,
    )
    if result.ok:
        return "No blockers: a disc put in now would be ripped."
    lines = []
    for blocker in result.blockers:
        lines.append(f"[BLOCKED] {blocker.title}")
        lines.append(f"          {blocker.detail}")
        if blocker.fix:
            lines.append(f"    fix:  {blocker.fix}")
    return "\n".join(lines)


def _checks(config) -> str:
    from adr import diagnostics

    result = diagnostics.run_checks(config)
    lines = []
    for check in result["checks"]:
        lines.append(f"[{check['status'].upper():4}] {check['title']}: {check['detail']}")
        if check["fix"]:
            lines.append(f"        fix: {check['fix']}")
    return "\n".join(lines)


def _storage(config) -> str:
    from adr import storage

    paths = {
        "raw": config.raw_path,
        "completed": config.completed_path,
        "staging": config.staging_path,
    }
    if config.plex_path:
        paths["plex"] = config.plex_path
    if config.tv_path:
        paths["tv"] = config.tv_path

    lines = []
    for name, path in paths.items():
        info = storage.describe_path(path)
        if not info["exists"]:
            lines.append(f"{name:10} {path}  MISSING")
            continue
        where = (
            "network" if info["is_network"]
            else "mount point" if info["is_mount"]
            else "inside a mount" if info["on_separate_filesystem"]
            else "container disk"
        )
        lines.append(
            f"{name:10} {path}  [{where}, {info['fstype'] or '?'}] "
            f"{'writable' if info['writable'] else 'NOT WRITABLE'}, "
            f"{info['free_gb']} GB free",
        )
    return "\n".join(lines)


def _drives() -> str:
    from adr.disc import diagnose_passthrough

    health = diagnose_passthrough()
    lines = []
    for drive in health["drives"]:
        lines.append(
            f"{drive['device']}  node={'yes' if drive['node_present'] else 'NO'} "
            f"openable={'yes' if drive['openable'] else 'NO'} "
            f"media={'yes' if drive.get('has_media') else 'no'}",
        )
    lines.extend(f"problem: {p}" for p in health["problems"])
    return "\n".join(lines) or "No optical drives found."


def _hardware(config) -> str:
    """Everything about hardware encoding, in one place.

    Diagnosing this from a distance took several rounds of "run this and paste
    the output" — the node, the group, the driver, the runtime, and finally
    what the stack itself says. All of it is cheap to gather and none of it
    can authenticate anything, so it belongs in the bundle rather than in a
    conversation.
    """
    from adr import gpu, vaapi
    from adr.encoderfactory import describe_backend
    from adr.encodertest import _preset_file, build_hardware_encoders

    state = gpu.describe()
    lines = [
        f"encoder      {describe_backend(config)}",
        f"vendor       {state['runtime'].get('vendor') or 'unknown'}",
        f"nodes        {', '.join(state['nodes']) or 'none'}",
        f"openable     {'yes' if state['available'] else 'NO'}",
        f"va drivers   {', '.join(state['runtime'].get('drivers', [])) or 'none'}",
        f"qsv runtime  {', '.join(state['runtime'].get('libs', [])) or 'none'}",
        f"dispatcher   {', '.join(state['runtime'].get('dispatchers', [])) or 'none'}",
        f"stack ok     {'yes' if state['runtime']['ok'] else 'NO'}",
        f"detail       {state['detail']}",
    ]

    encoders = build_hardware_encoders(config.handbrake_path)
    lines.append(f"hb encoders  {', '.join(encoders) or 'none (software-only build)'}")
    wanted = gpu.preset_wants_hardware(_preset_file(config), config.handbrake_preset)
    lines.append(f"preset wants {wanted or 'software'}")

    probe = gpu.vainfo()
    if probe["ran"]:
        lines.append(f"vainfo       {probe['driver'] or 'driver not named'}")
        lines.append(
            f"encode profs {', '.join(probe['encoders']) or 'NONE — cannot encode'}",
        )
    else:
        lines.append(f"vainfo       {probe['output']}")

    # Asked because HandBrake failing to reach the GPU says nothing about
    # whether the GPU can be reached — and the answer decides whether someone
    # gets hardware speed or an hour per film.
    elsewhere = vaapi.probe(config)
    lines.append(f"ffmpeg gpu   {elsewhere['detail']}")

    # The last question, and the only one the checks above cannot answer.
    #
    # "The driver is installed, the runtime is installed, ffmpeg encodes on
    # this GPU, and HandBrake says qsv is not available" is where every check
    # above runs out. The dispatcher knows which library it opened and why it
    # turned it down; it just has to be asked. Only worth asking when there is
    # something to explain — a working HandBrake needs no post-mortem.
    if wanted and not any("qsv" in name for name in encoders):
        dispatcher = gpu.qsv_dispatcher_log(config.handbrake_path)
        lines.append(f"qsv verdict  {dispatcher['summary']}")
        if dispatcher["log"]:
            lines.append("")
            lines.append("oneVPL dispatcher log (tail):")
            lines.append(dispatcher["log"].strip())
    return "\n".join(lines)


def _settings(config) -> str:
    """Every setting, with anything that could authenticate replaced."""
    lines = []
    data = config.as_dict()
    # An env-supplied key is set as far as the application is concerned, and
    # reporting it as empty sent one diagnosis looking for a missing key that
    # was there all along.
    with contextlib.suppress(Exception):
        data = {**data, **config.effective_secrets()}
    for key, value in sorted(data.items()):
        if key in SAFE_KEYS:
            lines.append(f"{key} = {value!r}")
        else:
            lines.append(f"{key} = {'<set, redacted>' if value else '<empty>'}")
    return "\n".join(lines)


#: How many jobs' raw directories to look inside, and how many files in each.
#: This spawns ffprobe per file, on the request thread, for a page someone is
#: waiting on.
RAW_AUDIO_JOBS = 3
RAW_AUDIO_FILES = 4


def _raw_audio(config) -> str:
    """What audio the ripped files actually carry, for the discs still on disk.

    Added because "the film came out with no sound" was answered three times
    by asking someone to run ffprobe by hand and paste the result. The answer
    is one line per file and it decides between two completely different
    problems: a disc that has no track in the wanted language, which the
    encoder now handles by asking for 'any' instead — and a disc that does
    have one, which means the audio was lost somewhere later and is a bug.

    Saltkråkan is why the distinction matters. It is a Swedish series, so
    "there is no Swedish track" sounds absurd — until you notice that plenty
    of Nordic DVDs carry a single audio track with no language tag at all, and
    an untagged track matches 'swe' exactly as poorly as an English one does.
    Nothing but the tags themselves tells those two apart.
    """
    from pathlib import Path

    from adr.encodingsettings import language
    from adr.models import Job, get_session
    from adr.vaapi import audio_streams

    wanted = language(config)
    exe = getattr(config, "ffmpeg_path", "") or "ffmpeg"
    session = get_session()
    try:
        jobs = session.query(Job).order_by(Job.id.desc()).limit(30).all()
        blocks = []
        for job in jobs:
            if len(blocks) >= RAW_AUDIO_JOBS:
                break
            raw_dir = Path(config.raw_path) / str(job.id)
            if not raw_dir.is_dir():
                continue
            try:
                files = sorted(p for p in raw_dir.glob("*.mkv") if p.is_file())
            except OSError:
                continue
            if not files:
                continue

            lines = [f"--- job #{job.id}: {job.display_title} ---"]
            for path in files[:RAW_AUDIO_FILES]:
                streams = audio_streams(exe, path)
                if not streams:
                    lines.append(f"  {path.name}: no audio tracks readable")
                    continue
                listing = ", ".join(
                    f"{index}:{stream.get('language') or 'untagged'}"
                    f" ({stream['codec']})"
                    for index, stream in enumerate(streams)
                )
                lines.append(f"  {path.name}: {listing}")
            if len(files) > RAW_AUDIO_FILES:
                lines.append(f"  ... and {len(files) - RAW_AUDIO_FILES} more")
            blocks.append("\n".join(lines))

        if not blocks:
            return "No ripped files are still on disk to inspect."

        if wanted:
            blocks.append(
                f"Wanted language: '{wanted}'. A track has to be tagged with it "
                "to be matched — 'untagged' and 'und' do not count, which is "
                "how a Swedish disc ends up with no Swedish track to find."
            )
        else:
            blocks.append(
                "No spoken language is set, so the disc's own track order "
                "decides and nothing here can fail to match."
            )
        return "\n\n".join(blocks)
    finally:
        session.close()


#: How many recent jobs to describe in full, and how many tracks of each.
RECENT_JOBS = 4
RECENT_TRACKS = 8


def _auto_eject(config) -> str:
    """Everything that decides whether the tray opens, in one place.

    Added because "why isn't the tray ejected?" could not be answered from a
    bundle at all: the setting was in one section, the drive in another, and
    what actually happened was in neither. Nothing here ejects anything — a
    diagnostic that opened the tray as a side effect would be a surprising
    thing to run while a disc is being read.
    """
    import shutil

    from adr.disc import eject_capability, media_status

    # Every drive the machine has, not only the ones someone named — an
    # unnamed drive is exactly as likely to be the one that will not open.
    from adr.disc import list_optical_drives

    try:
        drives = sorted(d["drive"] for d in list_optical_drives())
    except OSError:
        drives = []
    for named in sorted(getattr(config, "drive_labels", {}) or {}):
        if named not in drives:
            drives.append(named)
    if not drives:
        # Not a reason to stop: a container that has lost its passthrough is a
        # container whose trays certainly are not opening, and what the last
        # few jobs said about theirs is the useful half of the answer.
        return "No optical drives to report on.\n\n" + _eject_history(config)

    lines = []
    for device in drives:
        label = config.drive_label(device) or "(unnamed)"
        on = "on" if config.should_eject(device) else "OFF — Settings → Drives"
        capability = eject_capability(device)
        if capability["can_eject"] is True:
            tray = "the drive says it can open its tray"
        elif capability["can_eject"] is False:
            tray = (
                "THE DRIVE SAYS IT CANNOT OPEN A TRAY — a slot loader or a "
                "caddy accepts the command and has nothing to open"
            )
        else:
            tray = f"could not ask: {capability['detail']}"
        state = media_status(device)
        lines.append(
            f"{device}  {label}\n"
            f"    auto-eject : {on}\n"
            f"    tray       : {tray}\n"
            f"    right now  : {state['state']} — {state['detail']}"
        )

    binary = shutil.which("eject") or "/usr/bin/eject"
    have = "present" if Path(binary).exists() else "MISSING"
    lines.append(
        f"eject command: {binary} ({have}). The kernel is tried first and this "
        "is the fallback — in an LXC the command usually fails on its own, "
        "because it consults udev and there is none."
    )
    lines.append("")
    lines.append(_eject_history(config))
    return "\n".join(lines)


#: How many recent jobs are searched for what their tray actually did.
EJECT_HISTORY_JOBS = 8

#: The three things _eject_and_report can write, matched on the part of each
#: sentence that does not vary with the drive's name.
_EJECT_MARKS = ("Ejected the disc from", "Could not eject", "Leaving the disc in")


def _eject_history(config) -> str:
    """What the tray actually did on the last few jobs, out of their own logs.

    This section used to end by saying the answer was "below", in the recent
    jobs section. It was not: that section shows the tail of each job log, and
    the tail of any job that got as far as encoding is two dozen lines of
    HandBrake output. The eject line is thousands of lines above it, so the one
    question this whole section exists for could not be answered from a bundle
    — which is how an eject that only ever ran on the success path survived
    being reported twice.

    A job with no line at all is the finding, not a gap: it means nothing ever
    tried to open that tray.
    """
    from adr import joblog
    from adr.models import Job, get_session

    try:
        session = get_session()
    except Exception:                              # noqa: BLE001 - never fatal
        return "Could not open the database to read what the trays did."

    out = ["What the tray actually did, from each job's own log:"]
    try:
        jobs = (
            session.query(Job)
            .order_by(Job.id.desc())
            .limit(EJECT_HISTORY_JOBS)
            .all()
        )
        if not jobs:
            out.append("  no jobs yet, so no tray has been asked to open")
            return "\n".join(out)
        for job in jobs:
            said = []
            with contextlib.suppress(Exception):
                said = [
                    line for line in joblog.read(config, job.id).splitlines()
                    if any(mark in line for mark in _EJECT_MARKS)
                ]
            status = job.status.value if job.status else "unknown"
            if said:
                for line in said[-2:]:
                    out.append(f"  job #{job.id} [{status}] {line.strip()}")
            else:
                out.append(
                    f"  job #{job.id} [{status}] nothing in the log about the "
                    "tray — no eject was attempted for this disc"
                )
    finally:
        with contextlib.suppress(Exception):
            session.close()
    return "\n".join(out)


def _recent_jobs(config) -> str:
    """The last few jobs whatever their status, with what came off the disc.

    Every question this bundle has been sent to answer was about a job that
    *succeeded* and did the wrong thing — a film encoded silent, a two-minute
    clip filed as an episode, a tray that stayed shut. The failures section
    cannot show any of those, because none of them failed. This is the part
    that was missing: the durations and the episode numbers, which is what
    the naming decisions were actually made from.
    """
    from adr import joblog
    from adr.models import Job, get_session
    from adr.utils import format_duration

    session = get_session()
    try:
        jobs = (
            session.query(Job).order_by(Job.id.desc()).limit(RECENT_JOBS).all()
        )
        if not jobs:
            return "No jobs yet."

        blocks = []
        for job in jobs:
            status = getattr(job.status, "value", job.status)
            head = [
                f"--- job #{job.id}: {job.display_title} "
                f"[{status}] on {job.drive} ({job.content_type or 'movie'}) ---",
                f"label: {job.disc_label or '(none)'}"
                + (f"   season {job.series_season}" if job.series_season is not None else "")
                + (f"   from episode {job.series_first_episode}"
                   if job.series_first_episode else ""),
            ]
            tracks = list(job.tracks or [])
            if tracks:
                head.append(f"{len(tracks)} track(s):")
                for track in tracks[:RECENT_TRACKS]:
                    length = (
                        format_duration(track.duration_seconds)
                        if track.duration_seconds else "length unknown"
                    )
                    episode = (
                        f"E{track.episode_number:02d}"
                        if track.episode_number else "not an episode"
                    )
                    name = Path(track.output_path).name if track.output_path else "—"
                    head.append(
                        f"  {track.filename or '?'}  {length}  {episode}  "
                        f"{track.size_mb or 0:.0f} MB  -> {name}"
                    )
                if len(tracks) > RECENT_TRACKS:
                    head.append(f"  ... and {len(tracks) - RECENT_TRACKS} more")
            else:
                head.append("no tracks recorded")

            tail = joblog.read(config, job.id).splitlines()[-JOB_LOG_LINES:]
            head.append("job log:")
            head.extend(f"  {line}" for line in tail or ["(the log is empty)"])
            blocks.append("\n".join(head))
        return "\n\n".join(blocks)
    finally:
        session.close()


def _raw_usage(config) -> str:
    """What the raw directory is holding, per job.

    Until 1.31 nothing was ever deleted from it — every disc left its whole
    rip behind — and the bundle reported free space without ever saying what
    had taken the rest.
    """
    root = Path(config.raw_path)
    if not root.is_dir():
        return f"{root} does not exist."
    rows = []
    total = 0
    try:
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            size = sum(
                f.stat().st_size for f in child.rglob("*") if f.is_file()
            )
            total += size
            rows.append((child.name, size / 1024 / 1024))
    except OSError as exc:
        return f"Could not read {root}: {exc}"

    if not rows:
        return f"{root} is empty."
    rows.sort(key=lambda r: r[1], reverse=True)
    lines = [f"job {name}: {mb:,.0f} MB" for name, mb in rows[:12]]
    if len(rows) > 12:
        lines.append(f"... and {len(rows) - 12} more")
    lines.append(f"total: {total / 1024 / 1024:,.0f} MB in {len(rows)} job folder(s)")
    return "\n".join(lines)


def _failures(config) -> str:
    from adr import joblog
    from adr.models import Job, JobStatus, get_session

    session = get_session()
    try:
        jobs = (
            session.query(Job)
            .filter(Job.status == JobStatus.ERROR)
            .order_by(Job.id.desc())
            .limit(FAILED_JOBS)
            .all()
        )
        if not jobs:
            return "No failed jobs."
        blocks = []
        for job in jobs:
            head = [
                f"--- job #{job.id}: {job.display_title} on {job.drive} "
                f"({job.content_type or 'movie'}) ---",
                f"error: {job.error_message or '(none recorded)'}",
            ]
            for track in job.tracks:
                if track.error_message:
                    head.append(f"track {track.track_number}: {track.error_message}")
            tail = joblog.read(config, job.id).splitlines()[-JOB_LOG_LINES:]
            head.append("tool output:")
            head.extend(f"  {line}" for line in tail or ["(the log is empty)"])
            blocks.append("\n".join(head))
        return "\n\n".join(blocks)
    finally:
        session.close()


def _service_log(config) -> str:
    from adr import applog

    data = applog.read_tail(config, lines=SERVICE_LOG_LINES)
    if not data["exists"]:
        return f"(no log file at {data['path']} — is this an older install?)"
    return "\n".join(data["lines"]) or "(the log is empty)"
