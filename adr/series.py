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

from sqlalchemy.exc import SQLAlchemyError

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
#: "Disc 2", "D2", "DVD 2" — the marker that says which disc of a set this is.
#:
#: ``dvd`` comes first because alternation is ordered: without it the bare
#: ``d`` matched the *second* d of "dvd", which found the right number by
#: accident and then cut the show name in the wrong place — "Saltkråkan dvd 2"
#: parsed as show "Saltkråkan dv". Both discs of a set mangled it identically
#: so they still matched each other, which is exactly how a bug like this
#: survives being used.
#:
#: The lookarounds are what keep the bare ``d`` honest: without them
#: "Deadwood 2" matched on the d of "-wood" and claimed to be disc 2. They
#: check for an alphanumeric rather than using \b, because \b treats "_" as a
#: word character and every second disc label is SHOW_D2.
_DISC_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:dvd|disc|disk|d)[\s._-]*(\d{1,2})(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _hms(seconds: int) -> str:
    """Render a duration the way a person reads a disc title.

    Minutes-and-seconds alone turns a 2:16:00 feature into '136:00', which is
    exactly the number someone scanning the list to work out why detection went
    wrong does not want to have to divide by sixty.
    """
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def episode_candidates(title_info: dict[int, dict], config=None) -> list[int]:
    """Title indices that plausibly hold an episode, in disc order.

    ``title_info`` is what MakeMKV reports for the disc: index → {duration, …}.

    The thresholds come from *config* when given. They are a judgement about
    what television looks like, not a fact — anime runs to 24 minutes, a
    documentary series to 55, and someone's box set will sit outside whatever
    is chosen here. Making them settings means a wrong guess is a value to
    change rather than a patch to wait for.
    """
    from adr.utils import parse_duration

    low = int(getattr(config, "series_min_minutes", 0) or 0) * 60 or MIN_EPISODE_SECONDS
    high = int(getattr(config, "series_max_minutes", 0) or 0) * 60 or MAX_EPISODE_SECONDS
    min_count = int(getattr(config, "series_min_episodes", 0) or 0) or MIN_EPISODE_COUNT

    durations: dict[int, int] = {}
    for index, info in (title_info or {}).items():
        seconds = parse_duration(str(info.get("duration", "") or "")) or 0
        if low <= seconds <= high:
            durations[index] = seconds

    if len(durations) < min_count:
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

    if best_len < min_count:
        return []
    # Back to disc order: that is the order episodes are numbered in.
    return sorted(index for index, _ in ordered[best_start:best_start + best_len])


def looks_like_series(title_info: dict[int, dict], config=None) -> dict[str, Any]:
    """Is this a TV disc? Returns the guess and the reasoning behind it.

    ``{"is_series": bool, "episode_titles": [...], "confidence": float,
    "reason": str}``. The reason is shown to the user, because a wrong guess
    that explains itself is correctable and a wrong guess that does not is
    baffling.
    """
    candidates = episode_candidates(title_info, config)
    total = len(title_info or {})
    low = int(getattr(config, "series_min_minutes", 0) or 0) or MIN_EPISODE_SECONDS // 60
    high = int(getattr(config, "series_max_minutes", 0) or 0) or MAX_EPISODE_SECONDS // 60
    min_count = int(getattr(config, "series_min_episodes", 0) or 0) or MIN_EPISODE_COUNT

    # The durations it actually saw. A wrong verdict is otherwise undiagnosable:
    # "not enough similar titles" says nothing about which titles there were.
    from adr.utils import parse_duration
    seen = sorted(
        (parse_duration(str(i.get("duration", "") or "")) or 0)
        for i in (title_info or {}).values()
    )
    observed = ", ".join(_hms(s) for s in seen) or "none"

    if not candidates:
        return {
            "is_series": False,
            "episode_titles": [],
            "confidence": 0.0,
            "observed": observed,
            "reason": (
                f"{total} title(s) on the disc, but not {min_count} or more of "
                f"similar length between {low} and {high} minutes. Treating it as "
                f"a film. Title lengths seen: {observed}."
            ),
        }

    # More matching titles is stronger evidence; six episodes of 42 minutes is
    # not something a film disc produces by accident.
    confidence = min(0.95, 0.5 + 0.1 * (len(candidates) - min_count))
    return {
        "is_series": True,
        "episode_titles": candidates,
        "confidence": round(confidence, 2),
        "observed": observed,
        "reason": (
            f"{len(candidates)} titles of similar length ({low}–{high} min) look "
            f"like episodes rather than one main feature. Title lengths seen: "
            f"{observed}."
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


def episode_after_previous_discs(
    this_disc: int | None, previous: list[dict],
) -> tuple[int, str]:
    """Where disc *this_disc* should start numbering. ``(episode, why)``.

    Feeding a box set disc by disc numbers every disc from 1, because each is
    detected on its own and has no way of knowing what the last one used. The
    season folder then collects "Show - S01E01 (2).mp4" and the run is
    unusable. Series mode solves it by being told the show up front; this
    solves the case where nobody switched series mode on.

    The disc number is the only thing allowed to start it, and that is the
    whole safety argument. Continuing from "what is already in the season
    folder" alone cannot tell a second disc from a second *rip of the same
    disc*: both find five episodes there, and the re-rip would be silently
    filed as 6-10. A label that says D2 is a claim about which disc this is,
    and a re-rip of disc 1 says D1.

    *previous* is what the earlier discs of this show and season did:
    ``[{"disc": 1, "last_episode": 5}, ...]``. Nothing is invented from it —
    if the earlier discs left no episodes behind, this declines and says so,
    because "disc 3" on its own does not say how long discs 1 and 2 were.
    """
    if not this_disc or this_disc <= 1:
        return 1, ""

    seen = {int(entry["disc"]) for entry in previous if entry.get("disc")}
    if this_disc in seen:
        return 1, (
            f"The label says disc {this_disc}, and disc {this_disc} of this "
            "season has been ripped before — so this is the same disc again "
            "rather than the next one. Numbering starts at episode 1; change "
            "it above if that is wrong."
        )

    earlier = [
        int(entry["last_episode"]) for entry in previous
        if entry.get("disc") and int(entry["disc"]) < this_disc
        and entry.get("last_episode")
    ]
    if not earlier:
        return 1, (
            f"The label says disc {this_disc}, but nothing from an earlier "
            "disc of this season is on record, so there is no way to tell "
            "which episode it starts at. Numbering starts at 1 — change it "
            "above before encoding begins."
        )

    start = max(earlier) + 1
    return start, (
        f"The label says disc {this_disc}, and earlier discs of this season "
        f"ended at episode {max(earlier)}, so this one starts at episode "
        f"{start}. Change it above if the box set is not in disc order."
    )


def earlier_discs(session, show: str, job, season: int | None = None) -> list[dict]:
    """What earlier discs of this show and season did: disc number and last episode.

    Identity comes from the *parsed* show name rather than the raw label,
    because that is the part two discs of one box set agree on:
    ``SALTKRAKAN_D2`` and ``SALTKRAKAN_D3`` are the same programme and
    different strings. The season has to match too — season 2 disc 1 must not
    continue season 1.

    Only jobs that actually numbered something count. A disc that failed, or
    was ripped as a film, has nothing to say about where the next one starts.
    """
    out: list[dict] = []
    for other in _same_show_jobs(session, show, job, season):
        numbers = [
            t.episode_number for t in (other.tracks or [])
            if t.episode_number
        ]
        out.append({
            "disc": parse_series_label(other.disc_label or "")["disc"],
            "last_episode": max(numbers) if numbers else None,
        })
    return out


def _same_show_jobs(session, show: str, job, season: int | None = None) -> list:
    """Earlier series jobs for this show and season, newest first.

    The season is passed in rather than read off *job*, because the caller
    usually knows it before the row does. suggest_numbering answers for a disc
    that has not been marked as a series yet — ``job.series_season`` is still
    NULL — and takes the season off the label instead. Re-deriving it here
    read NULL as season 1, so a season-2 box set looked up season-1 discs:
    every set that was not season 1 was offered episode 1, and if season 1 of
    the same show happened to be in the library it was offered that season's
    numbers with a confident sentence about "this season".
    """
    from adr.models import Job

    wanted = (show or "").strip().casefold()
    if not wanted:
        return []
    if season is None:
        season = 1 if job.series_season is None else job.series_season
    season = int(season)
    try:
        candidates = (
            session.query(Job)
            .filter(Job.content_type == "series")
            .filter(Job.id != job.id)
            .filter(Job.series_season == season)
            .order_by(Job.id.desc())
            .limit(60)
            .all()
        )
    except SQLAlchemyError:
        # Narrow on purpose. This was `except Exception`, and it swallowed a
        # NameError from an import in the wrong function — answering "no
        # earlier discs", which is indistinguishable from a correct answer and
        # silently turned the continuation off.
        logger.warning("Could not look up earlier discs", exc_info=True)
        return []
    return [
        other for other in candidates
        if (parse_series_label(other.disc_label or "")["show"] or "")
        .strip().casefold() == wanted
    ]


def suggest_numbering(session, job) -> dict:
    """Where this disc should start, for anyone about to be asked.

    The pipeline works this out when it recognises a series on its own. Half
    the time nobody lets it: a disc is marked as a series by hand, from the
    dashboard, and that path set episode 1 every time however clearly the
    label said "dvd 2". The person doing the marking is exactly the person who
    should be shown the answer, so the same rule is available to them.

    ``apply`` is false once a job already carries a series number, so
    reopening the dialog cannot overwrite a choice someone made.
    """
    label = parse_series_label(getattr(job, "disc_label", "") or "")
    season = label["season"] or (
        1 if job.series_season is None else int(job.series_season)
    )
    first, why = episode_after_previous_discs(
        label["disc"], earlier_discs(session, label["show"], job, season),
    )
    already = (
        (job.content_type or "movie") == "series"
        and job.series_first_episode is not None
    )
    # And which show, when an earlier disc of the same set already answered
    # that. Cheaper and more certain than asking TMDb again, and it keeps the
    # discs of one box set on one spelling rather than two near-misses.
    show = ""
    year = None
    tmdb_id = None
    for other in _same_show_jobs(session, label["show"], job, season):
        if (other.title or "").strip():
            show, year, tmdb_id = other.title, other.year, other.tmdb_id
            break

    return {
        "season": int(season),
        "first_episode": int(first),
        "disc": label["disc"],
        "reason": why,
        "apply": bool(label["disc"] and not already),
        "show": show,
        "year": year,
        "tmdb_id": tmdb_id,
        "show_from": "an earlier disc of this set" if show else "",
    }


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
