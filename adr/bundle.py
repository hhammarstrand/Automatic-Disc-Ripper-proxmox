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

import logging
import platform
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
    "drives", "disabled_drives", "no_eject_drives", "drive_labels",
    "eject_after_rip", "main_feature_only", "log_level",
    "require_completed_mount", "stage_locally", "auto_move_to_plex",
    "series_detection", "series_min_minutes", "series_max_minutes",
    "series_min_episodes", "series_mode", "series_mode_show",
    "series_mode_season", "series_mode_next_episode",
    "skip_duplicates", "notify_enabled", "notify_provider", "notify_events",
    "plex_refresh_enabled", "plex_section",
    "audio_cd_enabled", "audio_cd_format", "audio_cd_mp3_bitrate",
    "cdparanoia_path", "ffmpeg_path", "data_disc_enabled",
    "web_host", "web_port",
})

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
    section(f"Last {FAILED_JOBS} failures", lambda: _failures(config))
    section(f"Service log (last {SERVICE_LOG_LINES} lines)", lambda: _service_log(config))
    return "\n".join(out) + "\n"


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
    from adr import gpu
    from adr.encodertest import _preset_file, build_hardware_encoders

    state = gpu.describe()
    lines = [
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
    return "\n".join(lines)


def _settings(config) -> str:
    """Every setting, with anything that could authenticate replaced."""
    lines = []
    for key, value in sorted(config.as_dict().items()):
        if key in SAFE_KEYS:
            lines.append(f"{key} = {value!r}")
        else:
            lines.append(f"{key} = {'<set, redacted>' if value else '<empty>'}")
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
