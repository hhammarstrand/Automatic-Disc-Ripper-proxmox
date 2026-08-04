"""What a setting is allowed to be.

The web API used to validate five settings out of forty-nine, in a growing
ladder of ``if "web_port" in data:`` blocks. The shape was the problem: adding
a rule meant adding a branch, so every setting added since simply never got
one. Type "abc" into the quality box and it was stored, then silently
discarded at the moment of use — the value was gone, nothing said so, and the
encode ran with the old one.

A table instead. Each rule is a name, a check and a sentence, and adding one
is a line rather than a branch. The sentence matters as much as the check: a
message has to say what was wrong *and* what would be right, because the
person reading it is looking at a box they have just typed into.

Nothing here coerces. The config properties already read defensively, and a
validator that quietly rewrote what someone typed would be the silent
discarding again in a different coat.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Quality is a quantiser, and 0 means "leave it to the preset". The window
#: is the one the encoders agree on; outside it the number stops meaning
#: anything useful in either.
QUALITY_RANGE = (15, 35)

#: Language codes as ISO 639 spells them, one form or the other.
LANGUAGE_LENGTHS = (2, 3)


def _integer(value, low, high, unit=""):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return f"must be a whole number{unit}"
    if not (low <= number <= high):
        return f"must be between {low} and {high}{unit}"
    return ""


def _one_of(value, allowed):
    if str(value) not in allowed:
        return "must be one of: " + ", ".join(repr(a) or "''" for a in allowed)
    return ""


def _quality(value):
    """0, or a quantiser. Two valid shapes, so it needs its own sentence."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "must be a whole number: 0 to leave it alone, or 15–35"
    if number == 0:
        return ""
    if not (QUALITY_RANGE[0] <= number <= QUALITY_RANGE[1]):
        return (f"must be 0 (leave it to the preset) or between "
                f"{QUALITY_RANGE[0]} and {QUALITY_RANGE[1]}")
    return ""


def _language(value):
    code = str(value or "").strip()
    if not code:
        return ""
    if len(code) not in LANGUAGE_LENGTHS or not code.isalpha():
        return "must be a language code such as 'swe', 'eng' or 'sv' — or empty"
    return ""


def _port(value):
    return _integer(value, 1, 65535)


def _path_or_empty(value):
    """Absolute, or empty. A relative path resolves against whatever the
    service's working directory happens to be, which is not something anyone
    means when they type one into a settings box."""
    text = str(value or "").strip()
    if text and not text.startswith("/"):
        return "must be an absolute path, starting with /"
    return ""


#: name -> check. A check returns "" when the value is fine, or the half of a
#: sentence that follows the setting's name.
RULES = {
    "web_port": _port,
    "max_encode_jobs": lambda v: _integer(v, 1, 64),
    "watch_interval": lambda v: _integer(v, 1, 3600, " of seconds"),
    "min_title_length": lambda v: _integer(v, 0, 86_400, " of seconds"),
    "log_level": lambda v: _one_of(v, ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")),
    "audio_cd_format": lambda v: _one_of(v, ("flac", "mp3")),
    "notify_provider": lambda v: _one_of(v, ("ntfy", "gotify", "discord", "webhook")),

    # Everything below here had no rule at all until this table existed.
    "encoder_backend": lambda v: _one_of(v, ("handbrake", "vaapi")),
    "vaapi_codec": lambda v: _one_of(v, ("h264", "hevc")),
    "libva_driver": lambda v: _one_of(v, ("", "iHD", "i965")),
    "video_quality": _quality,
    "max_height": lambda v: _integer(v, 0, 4320),
    "audio_language": _language,
    "series_min_minutes": lambda v: _integer(v, 1, 600),
    "series_max_minutes": lambda v: _integer(v, 1, 600),
    "series_min_episodes": lambda v: _integer(v, 2, 100),
    "audio_cd_mp3_bitrate": lambda v: _one_of(
        v, ("128k", "160k", "192k", "256k", "320k")),
    # Series mode counts across discs, so a negative one does not fail — it
    # names the next episode "S01E-1" and files it somewhere nobody looks.
    "series_mode_season": lambda v: _integer(v, 0, 100),
    "series_mode_next_episode": lambda v: _integer(v, 1, 1000),
    "series_mode_discs": lambda v: _integer(v, 0, 100),
    "completed_path": _path_or_empty,
    "raw_path": _path_or_empty,
    "staging_path": _path_or_empty,
    "plex_path": _path_or_empty,
    "tv_path": _path_or_empty,
    "music_path": _path_or_empty,
    "data_disc_path": _path_or_empty,
    "watch_path": _path_or_empty,
    "watch_output_path": _path_or_empty,
    "vaapi_device": _path_or_empty,
}


def check(data: dict) -> list[str]:
    """Every complaint about *data*, as whole sentences.

    All of them, not the first: someone who has just filled in a form wants
    to fix everything they got wrong in one pass, not discover the next
    mistake each time they press save.
    """
    problems = []
    for name, value in data.items():
        rule = RULES.get(name)
        if rule is None:
            continue
        try:
            complaint = rule(value)
        except Exception:                        # noqa: BLE001 - reported
            logger.exception("Rule for %r raised on %r", name, value)
            complaint = "could not be checked"
        if complaint:
            problems.append(f"{name} {complaint}")
    return problems


def cross_check(data: dict, current) -> list[str]:
    """Complaints that need more than one setting to see.

    A rule that only ever looks at one value cannot notice that the shortest
    episode is longer than the longest one — and that pair silently makes
    series detection match nothing at all.
    """
    problems = []

    def value(name):
        return data.get(name, getattr(current, name, None))

    try:
        shortest = int(value("series_min_minutes"))
        longest = int(value("series_max_minutes"))
        if shortest > longest:
            problems.append(
                f"series_min_minutes ({shortest}) is longer than "
                f"series_max_minutes ({longest}), so no episode can ever match")
    except (TypeError, ValueError):
        pass                                     # the per-field rules said so

    return problems
