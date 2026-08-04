"""Settings that mean the same thing whichever encoder runs.

Two encoders do the transcoding now, and until this module they were
configured in two unrelated ways: HandBrake from a preset file, ffmpeg from a
handful of ``vaapi_*`` settings. So "I want Swedish audio" was a checkbox in
one and a preset property in the other, and switching encoders silently
changed what you got. That is a bad way to arrange an application — the
settings should describe the *result*, not the tool.

Three things describe the result and both encoders can be told about them:

* **the language you want to hear** — the track that becomes the default one;
* **how big the picture may be** — a height cap, never an upscale;
* **quality** — a quantiser, lower being better.

Everything else stays where it belongs. HandBrake's preset holds a large body
of tuning that has no equivalent on the ffmpeg side, and pretending otherwise
would mean quietly discarding it. So each setting has an "leave it to the
preset" value, and that is the default: nothing here overrides a preset unless
someone asks it to.

The arguments are built here rather than in the encoders so the encoder test
can run *exactly* what a real encode would. A flag that HandBrake rejects then
shows up in two seconds instead of after a rip.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Quality as both encoders mean it: a quantiser, lower being better. 0 is
#: "say nothing about it", which for HandBrake means the preset decides.
QUALITY_RANGE = (15, 35)

#: What a quality number means, in words, for the UI. Deliberately vague at
#: the ends: the exact number that looks good depends on the encoder and the
#: source, and a table pretending otherwise would be false precision.
QUALITY_HINTS = {
    "low": "smaller files, visible artefacts on detailed scenes",
    "balanced": "close to the source at roughly half the size",
    "high": "very close to the source, noticeably larger files",
}


def clamp_quality(value: object) -> int:
    """A quality number, or 0 for "leave it alone"."""
    try:
        quality = int(value)
    except (TypeError, ValueError):
        return 0
    if quality <= 0:
        return 0
    return max(QUALITY_RANGE[0], min(QUALITY_RANGE[1], quality))


def handbrake_overrides(config) -> list[str]:
    """The command-line flags that impose the shared settings on HandBrake.

    HandBrake applies its own flags *after* the preset, so each of these
    replaces the corresponding preset value and leaves the rest of it intact.
    That is the whole reason this can exist without throwing a tuned preset
    away.

    Nothing is emitted for a setting left at its default, so an installation
    that has never touched this page produces exactly the command it produced
    before.
    """
    args: list[str] = []

    language = _language(config)
    if language:
        # --all-audio rather than the first match, so the other languages on
        # the disc still come across; the list decides which one leads.
        args += ["--audio-lang-list", language, "--all-audio"]

    height = _int(getattr(config, "max_height", 0))
    if height:
        # HandBrake never upscales, so this is a cap rather than a size.
        args += ["--maxHeight", str(height)]

    quality = clamp_quality(getattr(config, "video_quality", 0))
    if quality:
        args += ["-q", str(quality)]

    return args


def describe(config) -> str:
    """One line saying what the shared settings will do, for the UI."""
    parts = []
    language = _language(config)
    parts.append(f"audio in {language}" if language else "the disc's own audio order")
    height = _int(getattr(config, "max_height", 0))
    parts.append(f"capped at {height}p" if height else "the source resolution")
    quality = clamp_quality(getattr(config, "video_quality", 0))
    parts.append(f"quality {quality}" if quality else "the preset's own quality")
    return ", ".join(parts)


def _language(config) -> str:
    from adr.vaapi import normalise_language

    return normalise_language(getattr(config, "audio_language", "") or "")


def _int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
