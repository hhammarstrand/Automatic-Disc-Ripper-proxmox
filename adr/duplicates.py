"""Has this film been ripped before?

Working through a shelf of discs, the expensive mistake is ripping one twice:
forty minutes and several gigabytes for a file that is already in the library,
plus a duplicate for Plex to be confused by.

Three ways to answer, in descending order of authority:

1. **The library.** Does ``Title (Year)/`` already hold a video file at the
   destination? This is the only check that is actually true rather than
   remembered — it survives a cleared history, a reinstall, and catches films
   that were in the library before this application existed.
2. **A completed job with the same TMDb id.** Catches a different pressing of
   the same film, which the disc label alone never would.
3. **A completed job with the same disc label.** The fallback for a disc TMDb
   could not identify.

The check runs *after* identification, because before it the only thing known
about the disc is its label — which is the weakest of the three signals.

What to do about a duplicate is a separate question, and the answer is not
obviously "skip": re-ripping is legitimate when the first rip was from a
scratched disc, or at a worse preset. So the default is to say so loudly and
continue, with skipping available for someone deliberately working through a
large shelf.
"""

import contextlib
import logging
from pathlib import Path

from adr.models import Job, JobStatus

logger = logging.getLogger(__name__)

# What was matched. Ordered by how much it should be trusted.
MATCH_LIBRARY = "library"
MATCH_TMDB = "tmdb"
MATCH_LABEL = "label"

VIDEO_SUFFIXES = (".mp4", ".mkv", ".m4v", ".avi")


def _library_match(job, config, session=None) -> dict | None:
    """A folder for this title already holding video at the destination.

    Uses the same naming that a rip would produce, so this is literally asking
    "would this rip land on top of something?".
    """
    from adr.naming import plan_output
    from adr.pipeline import final_destination

    if not job.title:
        return None

    plan = plan_output(job, 1)
    try:
        parent, _ = final_destination(job, config)
    except (AttributeError, TypeError):
        return None

    folder = Path(parent) / plan.folder
    if not folder.is_dir():
        return None

    try:
        existing = sorted(
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
        )
    except OSError:
        return None
    if not existing:
        # An empty folder is a failed attempt, not a finished film.
        return None

    # Whose files are these?
    #
    # A folder holding video is normally a film that is already in the library.
    # But a job that failed or was cancelled during the transfer leaves its
    # output exactly there — and calling that a duplicate means the retry is
    # skipped, so the disc that failed once can never be ripped again without
    # turning the setting off. The rip that produced these files knows whether
    # it finished.
    owner = _unfinished_owner(folder, session)
    if owner is not None:
        logger.info(
            "%s holds files from job %s, which did not finish — not a duplicate",
            folder, owner,
        )
        return None

    return {
        "kind": MATCH_LIBRARY,
        "job_id": None,
        "path": str(folder),
        "files": [p.name for p in existing],
        "detail": (
            f"{folder} already exists and holds {len(existing)} video file(s): "
            f"{', '.join(p.name for p in existing[:3])}"
            f"{'…' if len(existing) > 3 else ''}."
        ),
    }



def _unfinished_owner(folder: Path, session) -> int | None:
    """The id of a job that wrote to *folder* and did not finish, if any.

    Never raises: a duplicate check that fails is not a reason to abandon a
    disc, and the honest answer when the database cannot be read is "no
    evidence that these are leftovers".
    """
    if session is None:
        return None
    try:
        wanted = str(Path(folder).resolve())
        for job in (
            session.query(Job)
            .filter(Job.status != JobStatus.DONE)
            .order_by(Job.id.desc())
            .limit(50)
        ):
            for candidate in (job.output_path, job.plex_path):
                if not candidate:
                    continue
                with contextlib.suppress(OSError):
                    if str(Path(candidate).resolve()) == wanted:
                        return job.id
    except Exception:                              # noqa: BLE001 - never fatal
        logger.debug("Could not check who owns %s", folder, exc_info=True)
    return None


def _tmdb_match(job, session) -> dict | None:
    """An earlier completed job for the same film, whatever disc it came from."""
    if not job.tmdb_id:
        return None
    previous = (
        session.query(Job)
        .filter(
            Job.id != job.id,
            Job.tmdb_id == job.tmdb_id,
            Job.status == JobStatus.DONE,
        )
        .order_by(Job.completed_at.desc())
        .first()
    )
    if not previous:
        return None
    when = f" on {previous.completed_at:%Y-%m-%d}" if previous.completed_at else ""
    return {
        "kind": MATCH_TMDB,
        "job_id": previous.id,
        "path": previous.output_path or "",
        "files": [],
        "detail": (
            f"'{previous.display_title}' was already ripped as job "
            f"{previous.id}{when} — same film, possibly a different disc."
        ),
    }


def _label_match(job, session) -> dict | None:
    """The original check: an earlier completed job with the same disc label."""
    from adr.pipeline import find_previous_rip

    previous = find_previous_rip(job, session)
    if not previous:
        return None

    # A disc label is the weakest signal, so better evidence overrules it. If
    # TMDb identified both discs and said they are different films, they are
    # different films — two pressings sharing a label is a coincidence, not a
    # duplicate.
    if job.tmdb_id and previous.tmdb_id and job.tmdb_id != previous.tmdb_id:
        return None

    when = f" on {previous.completed_at:%Y-%m-%d}" if previous.completed_at else ""
    return {
        "kind": MATCH_LABEL,
        "job_id": previous.id,
        "path": previous.output_path or "",
        "files": [],
        "detail": (
            f"A disc labelled '{job.disc_label}' was already ripped as job "
            f"{previous.id} ({previous.display_title}){when}. Ripping anyway — "
            "a disc label is not an identity, and recorders write the same one "
            "onto every disc they burn."
        ),
    }


def find_duplicate(job, session, config) -> dict | None:
    """The strongest evidence that this film is already ripped, or None.

    Series jobs are exempt: every disc of a box set legitimately writes into
    the same show folder, so a library match there is the normal case rather
    than a warning. Episodes are protected by their own numbering instead.
    """
    if (job.content_type or "movie") == "series":
        return None

    for check in (
        lambda: _library_match(job, config, session),
        lambda: _tmdb_match(job, session),
        lambda: _label_match(job, session),
    ):
        try:
            found = check()
        except Exception:
            # A duplicate check that raises must not stop a rip that would
            # otherwise have worked.
            logger.warning("Duplicate check failed", exc_info=True)
            continue
        if found:
            return found
    return None


#: Which kinds of evidence are strong enough to skip a rip on.
#:
#: Not the disc label. find_previous_rip's docstring has said since it was
#: written that a label "is not a unique identifier" and "only ever annotates a
#: job, never blocks one" — and then skip_duplicates blocked on it anyway.
#:
#: What that costs, in the report this constant exists for: a DVD recorder
#: writes the same volume label onto every disc it burns. One evening's worth
#: of home recordings all say LG_COMBI_RECORDER, so the first one ripped and
#: every one after it was cancelled as a duplicate of it — a film the user had
#: renamed by hand, which is the proof that the label never identified it.
#:
#: A library or TMDb match is different: both compare the *film*, and both are
#: worth acting on.
BLOCKING_MATCHES = frozenset({MATCH_LIBRARY, MATCH_TMDB})


def blocks_a_rip(match: dict | None) -> bool:
    """Whether this evidence is strong enough to cancel the rip."""
    return bool(match) and match.get("kind") in BLOCKING_MATCHES


def describe(match: dict) -> str:
    """One line for the log, the job log and the notification."""
    if not match:
        return "No earlier rip of this film was found."
    return match["detail"]
