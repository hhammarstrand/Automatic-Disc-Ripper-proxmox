"""Self-checks for the running container — the in-app half of `adr-doctor`.

Everything here is something the container can determine about itself. The
checks the container *cannot* run — the device cgroup, the passthrough entries,
guest-autostart ordering — need `pct` and therefore the Proxmox host, and the
page says so and hands over the command rather than pretending.

Each check returns a dict:

    {"id", "title", "status": "ok" | "warn" | "fail", "detail", "fix"}

*fix* is a command or a sentence the user can act on, or "" when the check
passed. A check that cannot suggest an action is not worth showing.
"""

import logging
import os
import shutil
from pathlib import Path

from adr.disc import diagnose_passthrough
from adr.storage import SERVICE_UID, check_destination, describe_path

logger = logging.getLogger(__name__)


def _check(id_: str, title: str, status: str, detail: str, fix: str = "") -> dict:
    return {"id": id_, "title": title, "status": status, "detail": detail, "fix": fix}


def check_drives() -> dict:
    """Can this container see and open the host's optical drives?"""
    health = diagnose_passthrough()
    drives = health["drives"]

    if health["ok"]:
        usable = [d["device"] for d in drives if d["openable"]]
        return _check(
            "drives", "Optical drives", "ok",
            f"{len(usable)} drive(s) usable: {', '.join(usable)}" if usable else "No drives.",
        )

    # The host-side fix is the same for every one of these, and it is the only
    # thing that can help — the container cannot repair its own passthrough.
    return _check(
        "drives", "Optical drives", "fail",
        " ".join(health["problems"]),
        "adr-doctor --fix {ctid}",
    )


def check_tools() -> dict:
    """MakeMKV and HandBrake — without them nothing rips or encodes."""
    missing = [name for name in ("makemkvcon", "HandBrakeCLI") if not shutil.which(name)]
    if not missing:
        return _check("tools", "MakeMKV and HandBrake", "ok", "Both are installed.")
    return _check(
        "tools", "MakeMKV and HandBrake", "fail",
        f"Not installed: {', '.join(missing)}. "
        "Discs cannot be ripped or encoded until they are.",
        "pct exec {ctid} -- /opt/adr/scripts/update.sh",
    )


def check_makemkv_key() -> dict:
    """MakeMKV refuses to read a disc without a registration key."""
    from adr.makemkv_key import read_existing_key

    try:
        key = read_existing_key()
    except OSError as exc:
        return _check("makemkv_key", "MakeMKV key", "warn", f"Could not read the key: {exc}")

    if key:
        return _check("makemkv_key", "MakeMKV key", "ok", "A registration key is stored.")
    return _check(
        "makemkv_key", "MakeMKV key", "fail",
        "No registration key. MakeMKV will refuse to open a disc.",
        "Settings → Refresh MakeMKV key",
    )


def describe_preset(config) -> dict:
    """Inspect the configured HandBrake preset file.

    A preset name that does not exist in the file it is supposed to come from
    makes every encode fail identically, and the only clue is HandBrake's
    output. Answering it here turns that into one line on the Doctor page.
    """
    import json

    preset_file = getattr(config, "handbrake_preset_file", "") or ""
    preset_name = getattr(config, "handbrake_preset", "") or ""
    info = {
        "preset_name": preset_name,
        "preset_file": preset_file,
        "file_exists": False,
        "valid_json": False,
        "preset_names_in_file": [],
        "name_match": False,
        "error": None,
    }

    if not preset_file:
        # Not an error: HandBrake's built-in presets are the common case.
        info["error"] = "No preset file configured (handbrake_preset_file is empty)"
        return info

    if not os.path.isfile(preset_file):
        info["error"] = f"File not found: {preset_file}"
        return info
    info["file_exists"] = True

    try:
        with open(preset_file, encoding="utf-8") as fh:
            data = json.load(fh)
        info["valid_json"] = True

        from adr.encoder import HandBrakeEncoder
        names: list[str] = []
        seen: set[str] = set()
        preset_list = data.get("PresetList", [])
        if isinstance(preset_list, list):
            for entry in preset_list:
                HandBrakeEncoder._extract_preset_names(entry, names, seen)
        # Flat format: the preset is the top-level object.
        if "PresetName" in data and data["PresetName"] not in seen:
            names.append(data["PresetName"])

        info["preset_names_in_file"] = names
        info["name_match"] = preset_name in names
        if not info["name_match"] and names:
            info["error"] = (
                f"Preset '{preset_name}' not found in file. "
                f"Available presets: {', '.join(names)}"
            )
    except json.JSONDecodeError as exc:
        info["error"] = f"Invalid JSON: {exc}"
    except (OSError, KeyError, TypeError) as exc:
        info["error"] = str(exc)

    return info


def check_preset(config) -> dict:
    """The HandBrake preset, which decides whether any encode can run at all."""
    info = describe_preset(config)

    if not info["preset_file"]:
        return _check(
            "preset", "HandBrake preset", "ok",
            f"Using HandBrake's built-in preset '{info['preset_name']}'.",
        )
    if not info["file_exists"]:
        return _check(
            "preset", "HandBrake preset", "fail", info["error"],
            "Settings → HandBrake preset file",
        )
    if not info["valid_json"]:
        return _check(
            "preset", "HandBrake preset", "fail",
            f"{info['preset_file']} is not valid JSON: {info['error']}",
            "Re-export the preset from the HandBrake GUI.",
        )
    if not info["name_match"]:
        return _check(
            "preset", "HandBrake preset", "fail", info["error"] or
            f"'{info['preset_name']}' is not in {info['preset_file']}.",
            "Settings → HandBrake preset",
        )
    return _check(
        "preset", "HandBrake preset", "ok",
        f"'{info['preset_name']}' found in {info['preset_file']}.",
    )


def check_destination_path(config) -> dict:
    """The path finished films are actually written to.

    With a Plex library and auto-move on, that is the library — not
    completed_path, which such a setup never touches.
    """
    to_plex = bool(config.plex_path and config.auto_move_to_plex)
    destination = config.plex_path if to_plex else config.completed_path
    label = "Plex library" if to_plex else "Completed folder"

    ok, error = check_destination(destination, require_mount=config.require_completed_mount)
    if ok:
        info = describe_path(destination)
        where = "network storage" if info["is_network"] else "local disk"
        return _check(
            "destination", f"Destination ({label})", "ok",
            f"{destination} — {where}, {info['free_gb']} GB free.",
        )
    return _check(
        "destination", f"Destination ({label})", "fail", error,
        "Storage page, or Settings → destination folder",
    )


def check_scratch(config) -> dict:
    """Rips and encodes both need room on the container's own disk."""
    problems = []
    for label, path in (("raw", config.raw_path), ("staging", config.staging_path)):
        ok, error = check_destination(path)
        if not ok:
            problems.append(f"{label}: {error}")

    if problems:
        return _check(
            "scratch", "Local scratch space", "fail", " ".join(problems),
            "Free space on the container disk, or grow it: pct resize {ctid} rootfs +20G",
        )

    raw = describe_path(config.raw_path)
    free = raw["free_gb"]
    # A dual-layer DVD is ~8.5 GB of MKV, and the encode needs room beside it.
    if free is not None and free < 20:
        return _check(
            "scratch", "Local scratch space", "warn",
            f"Only {free} GB free on the container disk. A dual-layer DVD needs "
            "about 8.5 GB of raw MKV plus the encode beside it.",
            "pct resize {ctid} rootfs +20G",
        )
    return _check("scratch", "Local scratch space", "ok", f"{free} GB free on the container disk.")


def check_database(config=None) -> dict:
    """The job database has to be writable or nothing is recorded."""
    from adr.config import DATABASE_PATH

    path = Path(DATABASE_PATH)
    parent = path.parent
    if not os.access(str(parent), os.W_OK):
        return _check(
            "database", "Job database", "fail",
            f"{parent} is not writable by the service user (uid {SERVICE_UID}).",
            f"chown -R adr:adr {parent}",
        )
    if path.exists() and not os.access(str(path), os.W_OK):
        return _check(
            "database", "Job database", "fail",
            f"{path} exists but is not writable by uid {SERVICE_UID}.",
            f"chown adr:adr {path}",
        )
    return _check("database", "Job database", "ok", f"{path} is writable.")


def run_checks(config) -> dict:
    """Run every self-check. Returns ``{"checks": [...], "ok": bool, "ctid": str|None}``.

    A failing check never aborts the rest — a broken drive should not hide a
    full disk.
    """
    checks: list[dict] = []
    for name, fn in (
        ("drives", lambda: check_drives()),
        ("tools", lambda: check_tools()),
        ("preset", lambda: check_preset(config)),
        ("makemkv_key", lambda: check_makemkv_key()),
        ("destination", lambda: check_destination_path(config)),
        ("scratch", lambda: check_scratch(config)),
        ("database", lambda: check_database(config)),
    ):
        try:
            checks.append(fn())
        except Exception as exc:
            logger.exception("Diagnostic check %s failed", name)
            checks.append(_check(name, name.replace("_", " ").title(), "warn",
                                 f"The check itself failed: {exc}"))

    ctid = os.environ.get("ADR_CTID", "").strip() or None
    for check in checks:
        if check["fix"]:
            check["fix"] = check["fix"].replace("{ctid}", ctid or "<CTID>")

    return {
        "checks": checks,
        "ok": all(c["status"] == "ok" for c in checks),
        "failing": sum(1 for c in checks if c["status"] == "fail"),
        "ctid": ctid,
    }
