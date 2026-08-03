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


def plan_output(job, file_count: int, fallback_title: str = "",
                fallback_year: int | None = None) -> OutputPlan:
    """Work out folder and filenames for *file_count* ripped titles.

    *fallback_title* is used when the job has no confident title — the parsed
    disc label — so a disc TMDb could not identify still lands somewhere
    sensible rather than under "Unknown".
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
    # Several titles from one film disc — a multi-part feature, or extras the
    # user chose to keep — get numbered parts, which Plex stacks.
    names = [folder] if count <= 1 else [f"{folder} - pt{i + 1}" for i in range(count)]
    return OutputPlan(folder=folder, filenames=names, episodes=[], is_series=False)
