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
