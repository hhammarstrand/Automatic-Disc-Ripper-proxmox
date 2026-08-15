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
from pathlib import Path

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


def handbrake_overrides(config, input_path=None) -> list[str]:
    """The command-line flags that impose the shared settings on HandBrake.

    HandBrake applies its own flags *after* the preset, so each of these
    replaces the corresponding preset value and leaves the rest of it intact.
    That is the whole reason this can exist without throwing a tuned preset
    away.

    Nothing is emitted for a setting left at its default, so an installation
    that has never touched this page produces exactly the command it produced
    before.

    *input_path* is the file about to be encoded, when there is one. It is
    what makes the language list safe — see :func:`audio_language_list`.
    """
    args: list[str] = []

    wanted = audio_language_list(config, input_path)
    if wanted:
        # The list only. How many matching tracks to take is the preset's
        # AudioTrackSelectionBehavior, and forcing --all-audio here would
        # override a deliberate choice with one nobody made: a preset that
        # says "first" wants one track, and handing it five is not a more
        # generous reading of the setting, it is a different setting.
        args += ["--audio-lang-list", wanted]

    height = _int(getattr(config, "max_height", 0))
    if height:
        # HandBrake never upscales, so this is a cap rather than a size.
        args += ["--maxHeight", str(height)]

    quality = clamp_quality(getattr(config, "video_quality", 0))
    if quality:
        args += ["-q", str(quality)]

    return args


#: HandBrake's "match every language", and the reason this module has to look
#: at the file at all.
#:
#: It is ``any`` and not ``und``. ``und`` is a real language in HandBrake's
#: table — Unknown — and matches only tracks actually tagged that way; ``any``
#: is a synthetic entry (``lang.c``: ``{"Any", "", "yy", "any"}``) that
#: ``find_audio_track`` compares against explicitly. Older presets had ``und``
#: rewritten to ``any`` on import, which is where the belief that they are
#: interchangeable comes from; for anything declaring a modern preset version
#: that rewrite no longer runs, so ``und`` on a disc tagged ``eng`` would go
#: on matching nothing.
ANY_LANGUAGE = "any"


def audio_language_list(config, input_path=None) -> str:
    """What to put in ``--audio-lang-list``: a language the file actually has.

    HandBrake does not fall back, and this is not an oversight that a flag
    somewhere turns off. ``hb_preset_job_add_audio`` only reaches for the
    wildcard when the language *list* is empty — never when the list is
    non-empty and matched nothing — so ``--audio-lang-list swe`` against a
    disc with no Swedish track selects no audio, writes the film silent, and
    exits 0. A successful encode of a silent movie. Old region-1 pressings
    are exactly that shape, which is why *The Black Cauldron*, *Jumanji* and
    *Charlotte's Web* all came out mute with the job saying Done.

    So the miss has to be detected here, before HandBrake runs. When the
    wanted language is on the file this returns it and nothing changes at all.
    When it is not, this returns ``any``, and the preset's own
    ``AudioTrackSelectionBehavior`` then does what it always does with it —
    ``first`` takes one track, the first non-commentary one.

    The disc still cannot answer in the language asked for; that is a fact
    about the disc and no flag changes it. The choice is only between the
    wrong language and no sound, and the wrong language is watchable.

    ``any`` rather than the first track's actual tag, which was the other
    candidate: the tag has to survive ffprobe and HandBrake reading it the
    same way, and if they disagree the result is silence again. ``any``
    cannot miss.

    Nothing readable means nothing changed — without ffprobe the wanted
    language goes out exactly as before, and the check after the encode is
    what catches the silence.
    """
    wanted = language(config)
    if not wanted or input_path is None:
        return wanted

    from adr.vaapi import audio_streams, language_matches

    exe = getattr(config, "ffmpeg_path", "") or "ffmpeg"
    streams = audio_streams(exe, Path(input_path))
    if not streams:
        return wanted
    if any(language_matches(s.get("language", ""), wanted) for s in streams):
        return wanted

    logger.info(
        "No '%s' audio in %s — asking HandBrake for '%s' instead so the film "
        "is not encoded silent", wanted,
        getattr(input_path, "name", input_path), ANY_LANGUAGE,
    )
    return ANY_LANGUAGE


def requested_language(overrides: list[str]) -> str:
    """The language a built override list ends up asking HandBrake for.

    Reading it back off the arguments rather than working it out a second time
    keeps the job log and the command that ran in step, and saves probing the
    file twice to answer one question.
    """
    try:
        return overrides[overrides.index("--audio-lang-list") + 1]
    except (ValueError, IndexError):
        return ""


def describe(config) -> str:
    """One line saying what the shared settings will do, for the UI.

    Worded against the encoder that will run, because "the preset's own
    quality" is a sentence about a file the GPU path never opens — and a
    status line that describes the wrong program is worse than none.
    """
    parts = []
    wanted = language(config)
    parts.append(f"audio in {wanted}" if wanted else "the disc's own audio order")

    height = _int(getattr(config, "max_height", 0))
    parts.append(f"capped at {height}p" if height else "the source resolution")

    quality = clamp_quality(getattr(config, "video_quality", 0))
    if quality:
        parts.append(f"quality {quality}")
    elif getattr(config, "encoder_backend", "handbrake") == "vaapi":
        parts.append("the encoder's default quality")
    else:
        parts.append("the preset's own quality")
    return ", ".join(parts)


#: Placeholders a HandBrake preset uses for "no preference". Treating them as
#: a language would pick the disc's first track and call it a decision.
_NO_LANGUAGE = frozenset({"und", "any", "", "none"})


def preset_language(config) -> str:
    """The spoken language the HandBrake preset asks for, normalised.

    HandBrake keeps this in the preset's ``AudioLanguageList``, and the ffmpeg
    backend never read it — so someone who had set Swedish in HandBrake, and
    then switched to the GPU encoder because HandBrake could not use the GPU,
    got English again with nothing anywhere saying why. The preset is the
    template; this is the part of it that the other encoder can honour.

    Only when the configured preset name is actually in the file. A file merely
    found in ``presets/`` while HandBrake resolves a built-in name is a
    different preset, and reading a language out of it would apply a setting
    from a preset that never runs.
    """
    import json

    from adr.diagnostics import describe_preset

    try:
        info = describe_preset(config)
    except Exception:                      # noqa: BLE001 - never break an encode
        logger.debug("Could not inspect the HandBrake preset", exc_info=True)
        return ""
    if not info.get("name_match") or not info.get("valid_json"):
        return ""

    try:
        with open(info["preset_file"], encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        logger.debug("Could not read %s", info.get("preset_file"), exc_info=True)
        return ""

    entry = _find_preset(data, info["preset_name"])
    if entry is None:
        return ""
    for code in entry.get("AudioLanguageList") or []:
        language = _normalise(str(code).strip().lower())
        if language and language not in _NO_LANGUAGE:
            return language
    return ""


def _find_preset(node, name: str):
    """The preset called *name* somewhere in a HandBrake preset file."""
    if isinstance(node, dict):
        if node.get("PresetName") == name and not node.get("Folder", False):
            return node
        for key in ("PresetList", "ChildrenArray"):
            found = _find_preset(node.get(key), name)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_preset(child, name)
            if found is not None:
                return found
    return None


def _normalise(code: str) -> str:
    from adr.vaapi import normalise_language

    return normalise_language(code)


def language(config) -> str:
    """The language to encode for: the setting, or the preset behind it.

    The setting wins because someone typed it. Falling back to the preset is
    what makes the two encoders agree — the alternative is that switching to
    the GPU silently changes the spoken language, which is what happened.
    """
    explicit = _normalise(getattr(config, "audio_language", "") or "")
    return explicit or preset_language(config)


def _int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
