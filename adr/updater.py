"""Checking for, and requesting, an update from GitHub.

The application runs as an unprivileged user with ``NoNewPrivileges=yes``, so it
cannot install its own update — and that is deliberate. The web UI is
unauthenticated on the LAN; a page that could escalate to root would be a much
bigger thing than a page that can rip a disc.

So the update is *requested*, not performed. The app touches a flag file;
``adr-update.path`` notices and starts ``adr-update.service``, which runs
``update.sh`` as root. What that unit fetches — repository and branch — lives in
the unit and in /etc/default/adr, never in the HTTP request. The most an
unauthenticated caller can do is ask for the update the machine's owner already
configured.

Running the update in its own unit also matters mechanically: ``update.sh``
stops and starts ``adr.service``, so an update spawned as a child of the web app
would kill itself halfway through.
"""

import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

INSTALL_DIR = Path(os.environ.get("ADR_INSTALL_DIR", "/opt/adr"))
COMMIT_FILE = INSTALL_DIR / ".commit"
REQUEST_FILE = INSTALL_DIR / ".update-requested"
LOG_FILE = INSTALL_DIR / "update.log"
UPDATE_UNIT = "adr-update.service"
WATCH_UNIT = "adr-update.path"

DEFAULT_REPO_URL = "https://github.com/hhammarstrand/Automatic-Disc-Ripper-proxmox.git"
DEFAULT_BRANCH = "main"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# The log can grow across updates; the UI only ever needs the tail.
_LOG_TAIL_BYTES = 16384


def repo_url() -> str:
    return os.environ.get("ADR_REPO_URL", "").strip() or DEFAULT_REPO_URL


def branch() -> str:
    return os.environ.get("ADR_BRANCH", "").strip() or DEFAULT_BRANCH


def installed_commit() -> str | None:
    """The commit this install was built from, or None if it was never recorded.

    ``update.sh`` deletes the .git directory after cloning — a working tree, not
    a checkout — so the SHA is written to a file at install time instead.
    """
    try:
        value = COMMIT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if _SHA_RE.match(value) else None


def remote_commit(url: str | None = None, ref: str | None = None,
                  timeout: int = 20) -> tuple[str | None, str]:
    """Ask GitHub for the current head of *ref*.

    ``git ls-remote`` rather than the REST API: no token, no rate limit, and it
    uses exactly the transport the update itself will use — so if this works,
    the update can fetch too.

    Returns ``(sha, error)``; *error* is empty on success.
    """
    url = url or repo_url()
    ref = ref or branch()
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", url, ref],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return None, "git is not installed in this container."
    except subprocess.TimeoutExpired:
        return None, f"GitHub did not answer within {timeout}s."
    except OSError as exc:
        return None, f"Could not reach GitHub: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return None, detail[-1] if detail else f"git ls-remote failed ({result.returncode})."

    for line in result.stdout.splitlines():
        sha = line.split("\t", 1)[0].strip()
        if _SHA_RE.match(sha):
            return sha, ""
    return None, f"Branch '{ref}' not found in {url}."


def check_for_update() -> dict:
    """Compare the installed commit with the branch head on GitHub.

    ``update_available`` is only ever True when both SHAs are known and differ.
    An unknown local commit means "cannot tell", not "out of date" — offering a
    phantom update on every page load would be worse than staying quiet.
    """
    current = installed_commit()
    latest, error = remote_commit()

    return {
        "current": current,
        "latest": latest,
        "repo_url": repo_url(),
        "branch": branch(),
        "update_available": bool(current and latest and current != latest),
        "known": current is not None,
        "error": error,
    }


def updates_supported() -> tuple[bool, str]:
    """Whether this install can update itself from the web UI.

    Two separate things have to be true, and both are worth checking: the unit
    must exist (installs made before in-app updates have no such unit), and the
    path unit that watches for the request must actually be running. A flag file
    nobody is watching would leave the button doing nothing at all, silently,
    which is the worst of the three outcomes.
    """
    if not (Path("/etc/systemd/system") / UPDATE_UNIT).exists():
        return False, (
            "This install predates in-app updates. Run the update once from the "
            "Proxmox host and the button will work from then on:  "
            "pct exec <CTID> -- /opt/adr/scripts/update.sh"
        )
    if not _unit_active(WATCH_UNIT):
        return False, (
            f"{WATCH_UNIT} is not running, so an update request would go "
            "unnoticed. On the Proxmox host: "
            f"pct exec <CTID> -- systemctl enable --now {WATCH_UNIT}"
        )
    return True, ""


def request_update() -> tuple[bool, str]:
    """Ask the root-side unit to run the update. Returns ``(ok, message)``."""
    supported, why = updates_supported()
    if not supported:
        return False, why

    state = update_status()
    if state["state"] in ("requested", "running"):
        return False, "An update is already in progress."

    try:
        REQUEST_FILE.touch()
    except OSError as exc:
        return False, f"Could not request the update: {exc}"
    logger.info("Update requested via %s", REQUEST_FILE)
    return True, "Update requested — the service will restart when it finishes."


def _unit_active(unit: str) -> bool:
    """Whether *unit* is active. Unprivileged; systemctl needs no root to read."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _unit_state() -> dict:
    """ActiveState/Result of the update unit, as far as an unprivileged user can see."""
    try:
        result = subprocess.run(
            ["systemctl", "show", UPDATE_UNIT,
             "--property=ActiveState", "--property=Result", "--property=ExecMainStatus"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def update_status() -> dict:
    """Where the update has got to, and what it has printed so far."""
    unit = _unit_state()
    active = unit.get("ActiveState", "")
    result = unit.get("Result", "")
    exit_status = unit.get("ExecMainStatus", "")

    if REQUEST_FILE.exists() and active != "activating":
        state = "requested"
    elif active == "activating":
        state = "running"
    elif (result and result != "success") or exit_status not in ("", "0"):
        state = "failed"
    elif active == "inactive" and exit_status == "0":
        state = "done"
    else:
        state = "idle"

    log = ""
    try:
        with open(LOG_FILE, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _LOG_TAIL_BYTES))
            log = fh.read().decode("utf-8", "replace")
    except OSError:
        pass

    return {
        "state": state,
        "active_state": active,
        "result": result,
        "log": log,
    }
