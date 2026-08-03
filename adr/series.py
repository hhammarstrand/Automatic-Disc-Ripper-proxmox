"""Television discs: recognising them, and naming what comes off them.

Everything else in this application assumes a disc is a film — one main
feature, named ``Title (Year)/Title (Year).mp4``. A box-set disc breaks that
assumption completely: six episodes of similar length, none of them a "main
feature", and Plex expects an entirely different layout.

Two problems, kept separate on purpose:

* **Recognising** a TV disc. Decided from the title durations, because that is
  all that is known before anything is ripped. It is a guess, and it is
  presented as one — the user confirms before encoding.
* **Naming** the episodes. Once the user has said "season 2, starting at
  episode 5", the mapping is arithmetic on titles sorted by index, which is the
  order they appear on the disc and almost always the broadcast order.

Plex's layout, which the naming here produces:

    Show Name (2019)/Season 02/Show Name (2019) - S02E05.mp4
"""

import logging
import re
from typing import Any

from adr.utils import sanitize_filename

logger = logging.getLogger(__name__)

# An episode is rarely shorter than 15 minutes or longer than 75. Below that is
# a menu loop, a trailer or a featurette; above it is a film or a play-all
# track, which would otherwise dominate the estimate.
MIN_EPISODE_SECONDS = 15 * 60
MAX_EPISODE_SECONDS = 75 * 60

# Fewer than this and "several similar-length titles" is not a pattern, it is a
# film with a making-of.
MIN_EPISODE_COUNT = 3

# Episodes of a season run to a broadcast slot and are close to the same
# length. Expressed as the widest ratio allowed between the longest and
# shortest member of a group: 1.35 admits a 42-minute episode beside a
# 31-minute one, and excludes a 22-minute episode beside a 16-minute
# featurette. The bound is on the *group*, not on a pivot — a pivot-relative
# window can bridge two genuinely separate clusters by sitting between them.
MAX_DURATION_RATIO = 1.35

_SxxExx_RE = re.compile(r"[Ss](\d{1,2})[\s._-]?[Ee](\d{1,3})")
_SEASON_WORD_RE = re.compile(
    r"(?:season|series|s(?:sn)?)[\s._-]*(\d{1,2})", re.IGNORECASE,
)
_DISC_RE = re.compile(r"(?:disc|disk|d)[\s._-]*(\d{1,2})", re.IGNORECASE)


def episode_candidates(title_info: dict[int, dict]) -> list[int]:
    """Title indices that plausibly hold an episode, longest run first.

    ``title_info`` is what MakeMKV reports for the disc: index → {duration, …}.
    """
    from adr.utils import parse_duration

    durations: dict[int, int] = {}
    for index, info in (title_info or {}).items():
        seconds = parse_duration(str(info.get("duration", "") or "")) or 0
        if MIN_EPISODE_SECONDS <= seconds <= MAX_EPISODE_SECONDS:
            durations[index] = seconds

    if len(durations) < MIN_EPISODE_COUNT:
        return []

    # Find the largest run of similar lengths, by scanning the durations in
    # sorted order and keeping the longest window whose longest and shortest
    # members are within MAX_DURATION_RATIO of each other. Sorting first is
    # what makes this a cluster rather than a pivot window: a group is only
    # accepted if *every* member is close to every other, so a featurette
    # cannot join four episodes just because it happens to sit near one of them.
    ordered = sorted(durations.items(), key=lambda kv: kv[1])
    best_start, best_len, best_ratio = 0, 0, float("inf")
    start = 0
    for end in range(len(ordered)):
        while ordered[end][1] > ordered[start][1] * MAX_DURATION_RATIO:
            start += 1
        length = end - start + 1
        ratio = ordered[end][1] / ordered[start][1]
        # Longer wins; equal length is broken by the tighter group, which is
        # the one more likely to be the episodes.
        if length > best_len or (length == best_len and ratio < best_ratio):
            best_start, best_len, best_ratio = start, length, ratio

    if best_len < MIN_EPISODE_COUNT:
        return []
    # Back to disc order: that is the order episodes are numbered in.
    return sorted(index for index, _ in ordered[best_start:best_start + best_len])


def looks_like_series(title_info: dict[int, dict]) -> dict[str, Any]:
    """Is this a TV disc? Returns the guess and the reasoning behind it.

    ``{"is_series": bool, "episode_titles": [...], "confidence": float,
    "reason": str}``. The reason is shown to the user, because a wrong guess
    that explains itself is correctable and a wrong guess that does not is
    baffling.
    """
    candidates = episode_candidates(title_info)
    total = len(title_info or {})

    if not candidates:
        return {
            "is_series": False,
            "episode_titles": [],
            "confidence": 0.0,
            "reason": (
                f"{total} title(s) on the disc, but not {MIN_EPISODE_COUNT} or more of "
                f"similar length between {MIN_EPISODE_SECONDS // 60} and "
                f"{MAX_EPISODE_SECONDS // 60} minutes. Treating it as a film."
            ),
        }

    # More matching titles is stronger evidence; six episodes of 42 minutes is
    # not something a film disc produces by accident.
    confidence = min(0.95, 0.5 + 0.1 * (len(candidates) - MIN_EPISODE_COUNT))
    return {
        "is_series": True,
        "episode_titles": candidates,
        "confidence": round(confidence, 2),
        "reason": (
            f"{len(candidates)} titles of similar length "
            f"({MIN_EPISODE_SECONDS // 60}–{MAX_EPISODE_SECONDS // 60} min) "
            "look like episodes rather than one main feature."
        ),
    }


def parse_series_label(disc_label: str) -> dict[str, Any]:
    """Pull a show name, season and disc number out of a disc label.

    Box-set labels are usually descriptive — ``THE_WIRE_S02_D3``,
    ``Firefly Season 1 Disc 2`` — and a correct default is one less thing for
    the user to type. Returns ``{"show", "season", "disc"}`` with None for
    anything not found.
    """
    raw = (disc_label or "").strip()
    if not raw:
        return {"show": "", "season": None, "disc": None}

    season = None
    disc = None

    match = _SxxExx_RE.search(raw)
    if match:
        season = int(match.group(1))
    else:
        match = _SEASON_WORD_RE.search(raw)
        if match:
            season = int(match.group(1))

    disc_match = _DISC_RE.search(raw)
    if disc_match:
        disc = int(disc_match.group(1))

    # Whatever precedes the season marker is the show name.
    show = raw
    for pattern in (_SxxExx_RE, _SEASON_WORD_RE, _DISC_RE):
        found = pattern.search(show)
        if found:
            show = show[: found.start()]
            break

    show = re.sub(r"[._]+", " ", show)
    show = re.sub(r"\s+", " ", show).strip(" -")
    return {"show": show.title() if show.isupper() else show, "season": season, "disc": disc}


def make_series_folder_name(show: str, year: int | None) -> str:
    """``Show Name (2019)`` — the top-level folder Plex expects."""
    safe = sanitize_filename(show)
    return f"{safe} ({year})" if year else safe


def make_season_folder_name(season: int) -> str:
    """``Season 02``. Zero is Plex's convention for specials."""
    return f"Season {int(season):02d}"


def make_episode_filename(show: str, year: int | None, season: int, episode: int) -> str:
    """``Show Name (2019) - S02E05``, without extension."""
    return f"{make_series_folder_name(show, year)} - S{int(season):02d}E{int(episode):02d}"


def episode_numbers(count: int, first_episode: int) -> list[int]:
    """Episode numbers for *count* titles starting at *first_episode*.

    Disc order is the order MakeMKV reports titles in, which is where they sit
    on the disc, which is broadcast order on every box set worth the name.
    """
    start = max(1, int(first_episode))
    return list(range(start, start + max(0, int(count))))
