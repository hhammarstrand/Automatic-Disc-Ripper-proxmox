"""Storage inspection helpers for the web UI's Storage page.

The mount itself always happens on the Proxmox HOST: this application runs
inside the container as the unprivileged 'adr' user and deliberately has no
ability to mount anything. What it CAN do — and what this module provides — is
tell the user the truth about where their files are actually landing:

  * is completed_path a real mount point, or just a directory on the container
    disk that looks identical until it fills up?
  * what is mounted there, and how much room is left?
  * can the service user actually write to it?

That last group of questions matters because of a genuinely surprising
behaviour: a bind-mount captures whatever the source resolved to when the
container started, and later host-side mounts do not propagate into a running
container. A NAS mounted after the container booted stays invisible, and rips
keep landing on the container disk with no error anywhere.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
from pathlib import Path
from typing import Any

# Only the two ports a NAS share actually listens on. Keeping this a fixed
# allow-list stops the (unauthenticated) probe endpoint from being usable as a
# general-purpose port scanner.
NAS_PORTS: dict[str, int] = {"nfs": 2049, "smb": 445}

# Must match ADR_UID/ADR_GID in scripts/install-container.sh — NFS authorises
# writes by numeric uid, so this number is what the user allows on the NAS.
SERVICE_UID = 8420


def _mount_info(path: str) -> tuple[str | None, str | None]:
    """Return (source, fstype) for the mount backing *path*, or (None, None).

    Parsed from /proc/self/mountinfo so it works without external tools.
    """
    try:
        target = os.path.realpath(path)
        best: tuple[int, str, str] | None = None
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split(" - ")
                if len(parts) != 2:
                    continue
                left, right = parts[0].split(), parts[1].split()
                if len(left) < 5 or len(right) < 2:
                    continue
                mountpoint = left[4]
                fstype, source = right[0], right[1]
                # Longest matching mountpoint wins (handles nested mounts).
                covers = target == mountpoint or target.startswith(mountpoint.rstrip("/") + "/")
                if covers and (best is None or len(mountpoint) > best[0]):
                    best = (len(mountpoint), source, fstype)
        if best:
            return best[1], best[2]
    except OSError:
        pass
    return None, None


def describe_path(path: str | Path) -> dict[str, Any]:
    """Describe a storage path: existence, mount status, capacity, writability.

    Never raises — a broken or unreachable path is reported, not thrown.
    """
    p = Path(path)
    info: dict[str, Any] = {
        "path": str(p),
        "exists": False,
        "is_mount": False,
        "on_separate_filesystem": False,
        "source": None,
        "fstype": None,
        "is_network": False,
        "writable": False,
        "total_gb": None,
        "free_gb": None,
        "used_percent": None,
        "error": None,
    }

    try:
        info["exists"] = p.is_dir()
    except OSError as exc:
        info["error"] = str(exc)
        return info

    if not info["exists"]:
        return info

    with contextlib.suppress(OSError):
        info["is_mount"] = os.path.ismount(str(p))

    # Whether the path lives on a filesystem of its own, rather than on the
    # container's root disk.
    #
    # This is the question "is my NAS actually attached" really asks, and
    # os.path.ismount answers a narrower one: it is true only for the mount
    # point itself. A library at /mnt/media/Filmer, inside a share mounted at
    # /mnt/media, is not a mount point — and organising a library into a
    # subfolder of the share is the normal thing to do. Comparing device ids
    # gets it right for the subfolder and still says no for a directory that
    # merely has the right name on the container disk.
    with contextlib.suppress(OSError):
        info["on_separate_filesystem"] = os.stat(str(p)).st_dev != os.stat("/").st_dev

    source, fstype = _mount_info(str(p))
    info["source"], info["fstype"] = source, fstype
    info["is_network"] = fstype in {"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs"} if fstype else False

    # os.access reflects the user this process runs as, which is the service
    # user — exactly the question we want answered.
    with contextlib.suppress(OSError):
        info["writable"] = os.access(str(p), os.W_OK | os.X_OK)

    try:
        usage = shutil.disk_usage(str(p))
        info["total_gb"] = round(usage.total / 1024**3, 1)
        info["free_gb"] = round(usage.free / 1024**3, 1)
        info["used_percent"] = round(usage.used / usage.total * 100, 1) if usage.total else None
    except OSError as exc:
        info["error"] = str(exc)

    return info


def check_destination(path: str | Path, require_mount: bool = False) -> tuple[bool, str]:
    """Check that finished files can actually be written to *path*.

    Called before a rip starts. Ripping a disc takes tens of minutes and
    produces several GB, so discovering at the very end that the destination
    is missing, read-only, or an unmounted NAS wastes the whole run.

    With *require_mount* the path must additionally live on attached storage
    rather than the container's own disk. ``adr-setup-nas`` turns that on,
    because for a NAS the difference between "mounted" and "an empty directory
    on the container disk" is invisible until the disk fills up.

    That is a question about the *filesystem the path is on*, not about the
    path being a mount point itself. A library at /mnt/media/Filmer inside a
    share mounted at /mnt/media is not a mount point, and putting the library
    in a subfolder of the share is the ordinary way to arrange one.

    Returns ``(ok, message)``; *message* is empty when ok.
    """
    info = describe_path(path)

    if not info["exists"]:
        return False, (
            f"Destination {info['path']} does not exist. "
            "Check 'Completed MP4 folder' under Settings."
        )

    if require_mount and not info["on_separate_filesystem"]:
        return False, (
            f"Destination {info['path']} is on the container's own disk, not on "
            "attached storage. The NAS share is not mounted, so finished files "
            "would fill the container disk instead. A bind-mount is captured "
            "when the container starts — if the NAS was mounted afterwards, "
            "restart the container."
        )

    if not info["writable"]:
        return False, (
            f"Destination {info['path']} is not writable by the service user "
            f"(uid {SERVICE_UID}). On an NFS share, allow that uid on the export."
        )

    # A dual-layer DVD needs ~8.5 GB of raw MKV plus the finished MP4.
    if info["free_gb"] is not None and info["free_gb"] < 5:
        return False, (
            f"Only {info['free_gb']} GB free on {info['path']} — not enough for a rip."
        )

    return True, ""


def should_stage(destination: str | Path, enabled: bool = True) -> bool:
    """Whether encoding should go to local scratch before being transferred.

    Only worth doing when the destination is network storage. HandBrake writes
    its output continuously, so encoding straight to a NAS keeps the share busy
    for the entire encode; staging locally turns that into one sequential
    transfer at the end. When the destination is already a local disk, staging
    would just be an extra copy of several GB, so it is skipped.
    """
    if not enabled:
        return False
    return bool(describe_path(destination)["is_network"])


def probe_nas(kind: str, host: str, timeout: float = 3.0) -> dict[str, Any]:
    """TCP-probe a NAS to confirm it is reachable and serving the right protocol.

    Read-only: opens a connection and closes it. Restricted to the NFS/SMB
    ports so this cannot be used to scan arbitrary services.
    """
    kind = (kind or "").lower()
    if kind not in NAS_PORTS:
        return {"ok": False, "error": f"Unsupported type '{kind}' — use nfs or smb."}
    host = (host or "").strip()
    if not host:
        return {"ok": False, "error": "No host given."}

    port = NAS_PORTS[kind]
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except socket.gaierror:
        return {"ok": False, "error": f"Could not resolve '{host}'. Check the name or use an IP."}
    except TimeoutError:
        return {"ok": False, "error": f"No answer from {host}:{port} within {timeout:g}s."}
    except OSError as exc:
        return {"ok": False, "error": f"{host}:{port} refused the connection ({exc.strerror or exc})."}
    return {"ok": True, "host": host, "port": port, "detail": f"{kind.upper()} server answered on port {port}."}


def build_nas_url(kind: str, host: str, share: str) -> str:
    """Build the NAS_URL the host-side helper expects."""
    share = "/" + (share or "").strip().strip("/")
    return f"{(kind or '').lower()}://{(host or '').strip()}{share}"


def _shell_single_quote(value: str) -> str:
    """Quote *value* for safe use inside single quotes in a POSIX shell."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def build_setup_command(
    kind: str,
    host: str,
    share: str,
    ctid: str | int | None = None,
    username: str = "",
    mountpoint: str = "",
    password: str = "",
) -> str:
    """Build the exact adr-setup-nas command to paste on the Proxmox host.

    For SMB the password can be supplied two ways:

    * omitted (recommended) — the command begins with a ``read -rsp`` prompt,
      so the password is typed on the host and never crosses the network or
      touches this application at all;
    * supplied — embedded in the command for a single copy-paste. Convenient,
      but it travels over plain HTTP to get here and is then visible on screen,
      so it is opt-in rather than the default.

    Either way nothing is stored: the value is used to render this string and
    then discarded.
    """
    url = build_nas_url(kind, host, share)
    is_smb = (kind or "").lower() == "smb"

    prefix = ""
    parts = [f"NAS_URL={url}"]

    if is_smb:
        parts.append(f"NAS_USERNAME={username or '<user>'}")
        if password:
            parts.append(f"NAS_PASSWORD={_shell_single_quote(password)}")
        else:
            # Prompt on the host instead of carrying the secret through here.
            prefix = "read -rsp 'SMB password: ' NAS_PASSWORD && echo\n"
            parts.append("NAS_PASSWORD=\"$NAS_PASSWORD\"")

    if mountpoint:
        parts.append(f"NAS_MOUNTPOINT={mountpoint}")
    parts.append(f"adr-setup-nas {ctid or '<CTID>'}")
    return prefix + " \\\n  ".join(parts)
