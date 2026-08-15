"""Whether a season is finished, and which episodes are still missing.

Feeding a box set one disc at a time, the question after every disc is the same
one: is that all of them? Nothing answered it. The disc came out, the folder
grew, and whether episode 9 existed anywhere was something to notice weeks
later in Plex.

**How many discs a set has cannot be looked up.** No metadata source knows it:
the number of discs is a property of one physical release in one region, and
TMDb describes programmes, not pressings. The same season ships as three discs
in Sweden and two in Germany.

What *can* be looked up is the episode list, which is the better question
anyway. Discs are packaging; episodes are what you wanted. Counting them also
catches a gap in the middle — a disc that failed halfway, a title skipped for
a navigation error — which counting discs never would.

So this compares the episodes on disk against the ones TMDb lists for the
season, and says what is missing in a sentence someone can act on.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: ``Show Name (1964) - S01E05.mp4`` — the only naming this application
#: produces for episodes, so the only one worth parsing.
_EPISODE_RE = re.compile(r"[Ss](\d{1,3})[Ee](\d{1,4})")

#: Containers an episode can be in. Transcoding off leaves MKVs.
_VIDEO = frozenset({".mp4", ".mkv", ".m4v"})


def episodes_on_disk(folder: Path, season: int) -> set[int]:
    """Episode numbers already in *folder*, for this season.

    The season is checked rather than assumed. A folder is normally one
    season, but ``Other/`` extras live inside it and a mis-numbered file from
    an earlier attempt can be anything at all.
    """
    found: set[int] = set()
    try:
        entries = list(folder.iterdir())
    except OSError:
        return found
    for path in entries:
        if not path.is_file() or path.suffix.lower() not in _VIDEO:
            continue
        match = _EPISODE_RE.search(path.stem)
        if match and int(match.group(1)) == int(season):
            found.add(int(match.group(2)))
    return found


def check(job, config, folder: Path | None = None) -> dict:
    """How complete this season is. ``{"known", "have", "expected", "missing", "text"}``.

    ``known`` is false whenever the answer would be a guess — no TMDb id
    because the show was named off the disc label, no API key, TMDb down, or a
    season it does not list. Saying nothing is right there: "0 of 0 episodes"
    reads as a fault, and the season may well be complete.
    """
    blank = {"known": False, "have": [], "expected": [], "missing": [], "text": ""}

    if (getattr(job, "content_type", "") or "movie") != "series":
        return blank

    season = int(1 if job.series_season is None else job.series_season)
    folder = folder or _season_folder(job)
    if folder is None:
        return blank

    have = episodes_on_disk(folder, season)

    from adr.identify import get_season_episodes

    api_key = getattr(config, "tmdb_api_key", "") or ""
    if not (job.tmdb_id and api_key):
        return {
            **blank,
            "have": sorted(have),
            "text": (
                f"{len(have)} episode(s) of season {season} are in the library. "
                "How many there should be is unknown — this show has no TMDb "
                "match, so name it from the dashboard to have the count "
                "checked."
            ) if have else "",
        }

    try:
        listed = get_season_episodes(job.tmdb_id, season, api_key)
    except Exception:                      # noqa: BLE001 - never fail a job
        logger.debug("TMDb season lookup failed", exc_info=True)
        listed = []

    expected = sorted(
        int(e["episode_number"]) for e in listed
        if e.get("episode_number") is not None
    )
    if not expected:
        return {**blank, "have": sorted(have), "text": ""}

    missing = sorted(set(expected) - have)
    if not missing:
        return {
            "known": True, "have": sorted(have), "expected": expected,
            "missing": [],
            "text": (
                f"Season {season} is complete: all {len(expected)} episodes "
                "are in the library."
            ),
        }

    return {
        "known": True, "have": sorted(have), "expected": expected,
        "missing": missing,
        "text": (
            f"Season {season} has {len(have)} of {len(expected)} episodes. "
            f"Still missing: {_runs(missing)}. Put the next disc in — the "
            "episode numbers carry on from what is already there."
        ),
    }


def _season_folder(job) -> Path | None:
    """Where this job's episodes ended up, or None."""
    for candidate in (getattr(job, "plex_path", None), getattr(job, "output_path", None)):
        if candidate:
            path = Path(str(candidate))
            if path.is_dir():
                return path
    return None


def _runs(numbers: list[int]) -> str:
    """``[9, 10, 11, 13]`` as ``"9-11, 13"``.

    Five missing episodes listed one by one is a wall of numbers; as a range
    it is a glance. Which matters, because this line exists to be read in a
    notification on a phone.
    """
    if not numbers:
        return ""
    parts: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(parts)
