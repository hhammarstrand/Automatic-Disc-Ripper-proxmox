"""Series mode: rip a whole box set without touching the UI between discs.

Marking one disc as television is a small thing. Doing it for six discs of a
season, each time re-entering the show, the season and — the part that actually
bites — the episode the disc starts at, is not. Get that last number wrong once
and a third of the season is misnumbered, which Plex will happily display as
the wrong episodes.

So series mode is a *sticky* answer to those questions plus a counter. Turn it
on with "The Wire, season 2, starting at episode 1", then feed discs. Each disc
takes the next block of episode numbers, and the counter advances by however
many titles that disc actually produced. Disc 2 continues at 5 because disc 1
yielded four episodes, not because anyone said so.

Two decisions worth stating, since both could reasonably go the other way:

* **It overrides detection.** The duration heuristic exists for when nobody has
  said what a disc is. Here someone has, explicitly, and they know better than
  a guess from title lengths.
* **It does not expire.** A mode that switched itself off after some interval
  would be surprising in the worst way — a film named as an episode, or worse,
  discs 4-6 of a season named from episode 1 again. It stays on until turned
  off, and every page says so in a banner that is hard to miss.
"""

import logging
import threading

logger = logging.getLogger(__name__)

# The counter is read-modify-written from a pipeline thread per drive, so two
# drives ripping at once would otherwise be able to hand the same episode
# numbers to both discs.
_lock = threading.Lock()


def is_active(config) -> bool:
    """Whether every inserted disc should be treated as this show's episodes."""
    return bool(getattr(config, "series_mode", False) and getattr(config, "series_mode_show", ""))


def state(config) -> dict:
    """The mode as the UI shows it."""
    return {
        "active": is_active(config),
        "show": getattr(config, "series_mode_show", "") or "",
        "year": getattr(config, "series_mode_year", None),
        "tmdb_id": getattr(config, "series_mode_tmdb_id", None),
        "season": int(getattr(config, "series_mode_season", 1) or 1),
        "next_episode": int(getattr(config, "series_mode_next_episode", 1) or 1),
        "discs_done": int(getattr(config, "series_mode_discs", 0) or 0),
    }


def start(config, show: str, season: int, first_episode: int = 1,
          year: int | None = None, tmdb_id: int | None = None) -> dict:
    """Turn the mode on. Returns the resulting state."""
    show = (show or "").strip()
    if not show:
        raise ValueError("A show name is required.")

    with _lock:
        config.update({
            "series_mode": True,
            "series_mode_show": show,
            "series_mode_year": int(year) if year else None,
            "series_mode_tmdb_id": int(tmdb_id) if tmdb_id else None,
            "series_mode_season": max(0, int(season)),
            "series_mode_next_episode": max(1, int(first_episode)),
            "series_mode_discs": 0,
        })
    logger.info(
        "Series mode on: %s season %s, next episode %s",
        show, season, first_episode,
    )
    return state(config)


def stop(config) -> dict:
    """Turn it off, keeping the show details so restarting is one click."""
    with _lock:
        config.update({"series_mode": False})
    logger.info("Series mode off")
    return state(config)


def apply_to(job, config) -> bool:
    """Stamp the mode's show, season and episode block onto *job*.

    Called when a job is created, before anything is ripped. Returns whether
    the mode was active. The episode counter is *not* advanced here: how many
    episodes this disc holds is not known until the rip finishes, and reserving
    a guessed block would leave gaps in the numbering when the guess was wrong.
    """
    if not is_active(config):
        return False

    current = state(config)
    job.content_type = "series"
    job.title = current["show"]
    job.year = current["year"]
    if current["tmdb_id"]:
        job.tmdb_id = current["tmdb_id"]
        # The poster would be whatever the film search found. It is not this
        # show, and showing it beside a correctly named episode is confusing.
        job.poster_url = None
    job.series_season = current["season"]
    job.series_first_episode = current["next_episode"]
    return True


def advance(config, episode_count: int) -> dict:
    """Move the counter past the episodes this disc produced.

    Called once the tracks are queued, which is the moment the numbers are
    actually spent. Doing it at insert time would mean guessing the count;
    doing it at completion would let two drives hand out the same numbers.
    """
    if not is_active(config) or episode_count <= 0:
        return state(config)

    with _lock:
        current = int(getattr(config, "series_mode_next_episode", 1) or 1)
        discs = int(getattr(config, "series_mode_discs", 0) or 0)
        config.update({
            "series_mode_next_episode": current + int(episode_count),
            "series_mode_discs": discs + 1,
        })
    new = state(config)
    logger.info(
        "Series mode: %d episode(s) taken, next disc starts at episode %d",
        episode_count, new["next_episode"],
    )
    return new


def set_next_episode(config, episode: int) -> dict:
    """Correct the counter by hand, for when a disc held bonus material.

    The count comes from how many titles were ripped, which is right until a
    disc includes a feature-length extra that looked like an episode. Being
    able to nudge it back is the difference between a small correction and
    re-ripping the rest of the season.
    """
    with _lock:
        config.update({"series_mode_next_episode": max(1, int(episode))})
    return state(config)


def describe(config) -> str:
    """One line for the log and the notification."""
    current = state(config)
    if not current["active"]:
        return "Series mode is off."
    year = f" ({current['year']})" if current["year"] else ""
    return (
        f"Series mode: {current['show']}{year}, season {current['season']}, "
        f"next episode {current['next_episode']} "
        f"({current['discs_done']} disc(s) done)."
    )
