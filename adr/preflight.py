"""Would a rip succeed if you put a disc in right now?

The pipeline has always refused to start a rip it knew would fail — a
destination that is missing, read-only, or an unmounted NAS wastes forty
minutes and several GB before anyone finds out. But it only ever said so
*after* a disc went in, once per disc, in a job that then sat red in the
history. Insert eleven discs and you get eleven identical failures and no
statement of the one thing wrong.

The same check now runs on demand, so the dashboard can say it once, before
the first disc, next to the drive you were about to use.

The point of this module is that the answer is produced by the *same code*
the pipeline gates on. A preflight that disagrees with the pipeline is worse
than none: it either promises a rip that then fails, or warns about one that
would have worked. `destination_blocker` is therefore the single
implementation, called from both.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from adr.storage import check_destination, should_stage

logger = logging.getLogger(__name__)


@dataclass
class Blocker:
    """One reason the next rip would not finish."""

    title: str
    detail: str
    #: What to do about it. A blocker nobody can act on is just bad news.
    fix: str = ""
    #: True when the pipeline refuses outright rather than failing later.
    stops_rip: bool = True


@dataclass
class Preflight:
    """What stands between this container and a finished film."""

    ok: bool = True
    blockers: list[Blocker] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "blockers": [asdict(b) for b in self.blockers]}


# ------------------------------------------------------------------ #
# The gate the pipeline itself uses
# ------------------------------------------------------------------ #

def destination_blocker(config) -> str | None:
    """Why finished files have nowhere to go, or None if they do.

    Checked in the order the files travel: the completed folder, then the Plex
    library if there is one, then the local staging area an encode needs when
    the destination is on the network.
    """
    ok, error = check_destination(
        config.completed_path, require_mount=config.require_completed_mount,
    )
    if not ok:
        return error

    # The Plex library is a real destination too — with auto_move_to_plex it is
    # the one a job will most likely use — so a broken library path has to fail
    # here, not after the encode.
    if config.plex_path:
        ok, error = check_destination(
            config.plex_path, require_mount=config.require_completed_mount,
        )
        if not ok:
            return f"Plex library unusable: {error}"

    # And the television library, which is where every series job actually
    # goes — final_destination routes on content_type alone, and series mode
    # stamps that at job creation, before this gate runs. It was never checked
    # here, so a box set could pass preflight against a healthy film library
    # and then be written onto the container's own disk, or fail on mkdir
    # after the whole rip: exactly what this module exists to prevent.
    if config.tv_path:
        ok, error = check_destination(
            config.tv_path, require_mount=config.require_completed_mount,
        )
        if not ok:
            return f"TV library unusable: {error}"

    # When encoding is staged locally the scratch area needs room too,
    # otherwise the rip only fails later, at the staging step.
    if should_stage(config.plex_path or config.completed_path, config.stage_locally):
        ok, error = check_destination(config.staging_path)
        if not ok:
            return f"Local staging area unusable: {error}"

    return None


# ------------------------------------------------------------------ #
# Everything the dashboard should warn about
# ------------------------------------------------------------------ #

def _destination_fix(detail: str) -> str:
    """Advice specific to how the destination is broken.

    "Not a mounted filesystem" and "not writable" have nothing in common
    except the word destination, and one piece of generic advice for both
    helps with neither.
    """
    if "own disk, not on attached storage" in detail:
        return (
            "A bind-mount is captured when the container starts. If the share was "
            "mounted on the host afterwards, restart the container: pct reboot {ctid}. "
            "If you meant to keep films on the container's own disk, point "
            "Settings → Completed folder at a local path, or turn off "
            "'Require the destination to be a mount point'."
        )
    if "not writable" in detail:
        return (
            "The share is attached but the service user cannot write to it. "
            "Re-run the NAS setup so the mount carries the right owner: "
            "pct exec {ctid} -- adr-setup-nas"
        )
    if "does not exist" in detail:
        return "Settings → Completed folder, or the Storage page to attach a share."
    if "not enough for a rip" in detail:
        return "Free some space at the destination, or point it somewhere larger."
    return "Storage page, or Settings → Completed folder."


def check(config, pipeline_manager=None) -> Preflight:
    """Everything that would stop the next disc finishing.

    Never raises. A preflight that throws would take the dashboard with it,
    and the dashboard is the thing meant to explain the problem.
    """
    result = Preflight()

    try:
        detail = destination_blocker(config)
    except Exception as exc:                      # noqa: BLE001 - reported
        logger.exception("Preflight destination check failed")
        detail = f"The destination could not be checked: {exc}"
    if detail:
        result.blockers.append(Blocker(
            title="Finished files have nowhere to go",
            detail=detail,
            fix=_destination_fix(detail),
        ))

    try:
        result.blockers.extend(_drive_blockers(pipeline_manager))
    except Exception:
        logger.exception("Preflight drive check failed")

    result.ok = not result.blockers
    return result


def _drive_blockers(pipeline_manager) -> list[Blocker]:
    """Reasons no disc can be read at all."""
    from adr.disc import diagnose_passthrough

    health = diagnose_passthrough()
    if health["ok"]:
        return []

    # Told apart because they need opposite actions: a drive the host cannot
    # see is a cable, a drive the host has but the container does not is the
    # passthrough.
    host_only = [d["device"] for d in health["drives"] if not d["node_present"]]
    if host_only:
        return [Blocker(
            title="The container cannot reach the drive",
            detail=(
                f"The host has {', '.join(host_only)} but this container does not. "
                "The passthrough did not apply at start."
            ),
            fix="Run on the Proxmox host: adr-doctor --fix {ctid}",
        )]
    return [Blocker(
        title="No optical drive is usable",
        detail=" ".join(health["problems"]) or "No drive answered.",
        fix="Run on the Proxmox host: adr-doctor --fix {ctid}",
    )]


def with_ctid(result: Preflight, ctid: str | None) -> Preflight:
    """Substitute the container id into the fixes, so commands are runnable."""
    for blocker in result.blockers:
        if blocker.fix:
            blocker.fix = blocker.fix.replace("{ctid}", ctid or "<CTID>")
    return result
