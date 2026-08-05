"""Deciding what the finished files are called, in one place.

This used to be four lines inline in the pipeline, which was fine while a disc
could only be a film. With television in the picture the decision has real
branches — a season folder, per-episode numbering, specials — and inline
branching in the middle of a 300-line method is where naming bugs live.

Plex's two layouts, which are what everything here produces:

    Movie (1999)/Movie (1999).mp4
    Show (2019)/Season 02/Show (2019) - S02E05.mp4
"""

from dataclasses import dataclass, field

from adr.series import (
    episode_numbers,
    make_episode_filename,
    make_season_folder_name,
    make_series_folder_name,
)
from adr.utils import make_plex_folder_name, sanitize_filename

#: What a finished file can be called. MP4 is what HandBrake produces; MKV is
#: what a job keeps when transcoding is turned off. Anything that looks for
#: "the finished files" has to accept both, or turning transcoding off silently
#: breaks renaming, retrying and duplicate detection.
OUTPUT_SUFFIXES = (".mp4", ".mkv")


def finished_files(directory) -> list:
    """Every finished video file directly inside *directory*, sorted."""
    from pathlib import Path

    path = Path(str(directory))
    if not path.is_dir():
        return []
    try:
        return sorted(
            p for p in path.iterdir()
            if p.is_file() and p.suffix.lower() in OUTPUT_SUFFIXES
        )
    except OSError:
        return []


@dataclass
class OutputPlan:
    """Where a job's files go and what they are called.

    *folder* is relative to the destination root, so the same plan works for a
    staging directory and for the final library without recomputation.
    """

    folder: str
    filenames: list[str] = field(default_factory=list)
    #: Episode number per file for a series, aligned with *filenames*; empty
    #: for a film. Stored on the tracks so a restart mid-encode keeps the
    #: mapping it started with.
    episodes: list[int] = field(default_factory=list)
    is_series: bool = False


def folder_depth(job) -> int:
    """How many path components below the destination root a job occupies.

    One for a film (``Movie (1999)/``), two for a series
    (``Show (2019)/Season 02/``). Everything that moves a finished folder —
    the transfer to the destination, the move into Plex — needs this, because
    taking only the last component of a series path would drop the show folder
    and scatter seasons across the library root.
    """
    return 2 if (job.content_type or "movie") == "series" else 1


def relative_folder(output_path, job) -> str:
    """The part of *output_path* that belongs below the destination root."""
    from pathlib import Path

    parts = Path(str(output_path)).parts
    depth = min(folder_depth(job), len(parts))
    return str(Path(*parts[-depth:])) if depth else ""


#: Where extras go inside a film's folder. Plex only recognises eight names —
#: Behind The Scenes, Deleted Scenes, Featurettes, Interviews, Scenes, Shorts,
#: Trailers, Other — and "Other" is the one that does not claim to know what
#: the extra is. MakeMKV gives us a duration and nothing else, so it is the
#: only honest choice.
EXTRAS_FOLDER = "Other"

#: How much longer the longest title must be than the next one before the rest
#: are called extras. Below this they are more likely to be parts of one film,
#: and calling half a film an extra is the worse mistake of the two.
MAIN_FEATURE_RATIO = 1.5

#: How many files can still plausibly be read as one film. Real multi-part
#: releases are two parts, occasionally three. Sixteen titles is a disc with
#: its extras on it, and there is no reading of sixteen numbered parts that
#: Plex handles well — it *stacks* them, so the trailers become the last
#: fourteen minutes of the film.
MAX_STACKED_PARTS = 3


def longest_title(durations) -> int | None:
    """Index of the longest title, ignoring the ones with no duration.

    Unlike :func:`pick_main_feature` this makes no claim about confidence: it
    answers "which of these is longest" and nothing else. It is for the cases
    where the question has already been settled elsewhere — the user asked for
    the main feature only, or there are more titles than any multi-part film
    has — and the only thing left to decide is which one.

    Returns None only when no title has a usable duration at all.
    """
    best: int | None = None
    best_value = 0.0
    for index, value in enumerate(durations):
        try:
            seconds = float(value or 0)
        except (TypeError, ValueError):
            continue
        if seconds > best_value:
            best, best_value = index, seconds
    return best


def pick_main_feature(durations) -> int | None:
    """Which of the ripped titles is the feature, or None when it is unclear.

    *durations* is one value per file, in seconds, aligned with the file list;
    None or zero means unknown. A missing duration anywhere returns None —
    guessing the feature from partial information is how a trailer ends up
    named as the film.
    """
    values = list(durations)
    if len(values) < 2 or any(not d for d in values):
        return None
    ranked = sorted(range(len(values)), key=lambda i: values[i], reverse=True)
    longest, runner_up = ranked[0], ranked[1]
    if values[longest] >= values[runner_up] * MAIN_FEATURE_RATIO:
        return longest
    return None


def largest_file(sizes) -> int | None:
    """Index of the biggest file, ignoring the ones with no size.

    The last resort for picking the feature, and a surprisingly good one: on a
    disc with a film and its extras the film is an order of magnitude larger,
    not a few percent. It exists because duration is read out of MakeMKV's
    TINFO records and matched to files by name, and every step of that can come
    back empty — at which point the alternative is to number sixteen files
    ``pt1``…``pt16`` and let Plex stack the trailers onto the film.
    """
    best: int | None = None
    best_value = 0.0
    for index, value in enumerate(sizes):
        try:
            size = float(value or 0)
        except (TypeError, ValueError):
            continue
        if size > best_value:
            best, best_value = index, size
    return best


def resolve_main_feature(durations, main_feature_only: bool, sizes=None) -> int | None:
    """Which of the ripped titles is the film, given what the user asked for.

    :func:`pick_main_feature` is deliberately timid — it only answers when the
    longest title stands well clear of the next one. Two things on an ordinary
    disc defeat it: a commentary version of the film, which is exactly as long
    as the film, and a title MakeMKV reported no duration for, which makes it
    decline outright. Either way every file ends up named "pt1"…"ptN", and Plex
    *stacks* numbered parts, so a disc with fifteen featurettes becomes one
    sixteen-part movie with the trailers on the end.

    So where the question has already been answered elsewhere, answer it. The
    user asking for the main feature only has said which title they want; a
    count past :data:`MAX_STACKED_PARTS` is past anything a real multi-part
    release reaches. In both cases "the longest one is the film" is not a
    guess, and it beats stacking the extras onto the end of it.
    """
    values = list(durations)
    index = pick_main_feature(values)
    if index is not None or len(values) < 2:
        return index
    if not (main_feature_only or len(values) > MAX_STACKED_PARTS):
        return None
    # Duration first, because it is what "the feature" means. Size only when
    # no duration is known at all — which happens whenever MakeMKV's TINFO
    # records cannot be matched to the files on disk, and used to end in
    # sixteen numbered parts.
    #
    # Written out rather than `a or b`: index 0 is a perfectly good answer and
    # a falsy one, and the film is the first title on a disc more often than
    # not.
    by_duration = longest_title(values)
    if by_duration is not None:
        return by_duration
    return largest_file(sizes or [])


def only_the_feature(files, durations, main_index: int | None):
    """Drop everything but the feature from a ripped title list.

    For the case where "main feature only" was on and the disc still produced
    several titles — the pre-rip scan is the only thing that could have
    prevented that, and when it comes back empty the setting has one place left
    to take effect. Encoding fifteen featurettes nobody asked for costs hours
    and fills the library with them.

    Nothing is deleted: the other titles stay in the job's raw directory as
    MKV, so a wrong call here is undone by re-encoding rather than by ripping
    the disc again.

    Returns ``(files, durations, main_index)`` unchanged when there is nothing
    to drop, and ``main_index`` as None once only the feature is left — a
    single file is the film by definition.
    """
    kept = list(files)
    lengths = list(durations)
    if main_index is None or len(kept) <= 1 or not 0 <= main_index < len(kept):
        return kept, lengths, main_index
    length = lengths[main_index] if main_index < len(lengths) else None
    return [kept[main_index]], [length], None


def plan_output(job, file_count: int, fallback_title: str = "",
                fallback_year: int | None = None,
                main_index: int | None = None) -> OutputPlan:
    """Work out folder and filenames for *file_count* ripped titles.

    *fallback_title* is used when the job has no confident title — the parsed
    disc label — so a disc TMDb could not identify still lands somewhere
    sensible rather than under "Unknown".

    *main_index* names which of the ripped titles is the feature, when the
    caller knows. Everything else then becomes an extra in ``Other/`` rather
    than a numbered part. The distinction matters: Plex *stacks* numbered
    parts into one film, so a two-minute trailer named "pt2" becomes the
    second half of the movie.
    """
    title = sanitize_filename(job.title or fallback_title or "Unknown")
    year = job.year if job.year is not None else fallback_year
    count = max(0, int(file_count))

    if (job.content_type or "movie") == "series":
        season = int(job.series_season or 1)
        first = int(job.series_first_episode or 1)
        numbers = episode_numbers(count, first)
        return OutputPlan(
            folder=f"{make_series_folder_name(title, year)}/{make_season_folder_name(season)}",
            filenames=[make_episode_filename(title, year, season, n) for n in numbers],
            episodes=numbers,
            is_series=True,
        )

    folder = make_plex_folder_name(title, year)
    if count <= 1:
        return OutputPlan(folder=folder, filenames=[folder], episodes=[], is_series=False)

    if main_index is not None and 0 <= main_index < count:
        names = []
        extra = 0
        for index in range(count):
            if index == main_index:
                names.append(folder)
            else:
                extra += 1
                names.append(f"{EXTRAS_FOLDER}/Extra {extra}")
        return OutputPlan(folder=folder, filenames=names, episodes=[], is_series=False)

    # Nobody could say which title is the feature, so treat them as parts of
    # one film — which is what a genuinely multi-part disc is.
    names = [f"{folder} - pt{i + 1}" for i in range(count)]
    return OutputPlan(folder=folder, filenames=names, episodes=[], is_series=False)
