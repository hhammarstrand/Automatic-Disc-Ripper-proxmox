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


def build_setup_command(
    kind: str,
    host: str,
    share: str,
    ctid: str | int | None = None,
    username: str = "",
    mountpoint: str = "",
) -> str:
    """Build the exact adr-setup-nas command to paste on the Proxmox host.

    The password is intentionally NOT included: it is never sent to this
    application, so the user fills it in on the host where it belongs.
    """
    url = build_nas_url(kind, host, share)
    parts = [f"NAS_URL={url}"]
    if (kind or "").lower() == "smb":
        parts.append(f"NAS_USERNAME={username or '<user>'}")
        parts.append("NAS_PASSWORD='<password>'")
    if mountpoint:
        parts.append(f"NAS_MOUNTPOINT={mountpoint}")
    parts.append(f"adr-setup-nas {ctid or '<CTID>'}")
    return " \\\n  ".join(parts)
