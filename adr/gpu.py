"""Can this container use hardware video encoding?

A preset exported from HandBrake on a desktop often asks for the encoder that
desktop had: Intel Quick Sync, VAAPI, NVENC. Inside an LXC none of those exist
unless the GPU was deliberately passed through, and HandBrake's answer is the
same for every title of every disc — ``encqsvInit: qsv is not available on the
system``, exit 3, forty minutes after the disc went in.

The question has a cheap answer. Hardware encoding on Linux goes through a DRM
render node, ``/dev/dri/renderD128``. If it is not in the container, no amount
of preset fiddling will help; if it is there but cannot be opened, the device
cgroup is denying it, or the service user is not in the right group. Those are
three different problems with three different fixes, and telling them apart
here is the difference between "use a software preset" — which throws away the
hardware — and "pass the GPU through", which is what the person actually
wanted when they picked that preset.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DRI_DIR = Path("/dev/dri")

#: The DRM character-device major. Needed by the host-side fix, and named here
#: so the one place that knows it is the one place that explains it.
DRM_MAJOR = 226

#: Encoder names that mean "this needs a GPU". Matched against a preset's
#: video encoder and against HandBrake's own complaints.
HARDWARE_ENCODERS = ("qsv", "nvenc", "vce", "vaapi", "videotoolbox", "mf_")

#: Where a VA-API driver lands. Multiarch first, because that is where Debian
#: and Ubuntu put it; the others are for distributions that do not use it.
VA_DRIVER_DIRS = (
    Path("/usr/lib/x86_64-linux-gnu/dri"),
    Path("/usr/lib/dri"),
    Path("/usr/lib64/dri"),
)

#: The VA driver Quick Sync and VAAPI both go through. iHD covers Broadwell
#: and later, i965 the older parts; either one means the stack is installed.
INTEL_VA_DRIVERS = ("iHD_drv_video.so", "i965_drv_video.so")


def render_nodes() -> list[str]:
    """Every render node visible in this container, sorted."""
    try:
        return sorted(
            str(p) for p in DRI_DIR.iterdir() if p.name.startswith("renderD")
        )
    except OSError:
        return []


def _openable(path: str) -> tuple[bool, int | None]:
    """Try to open *path*. Returns ``(ok, errno)``."""
    fd = None
    try:
        fd = os.open(path, os.O_RDWR)
        return True, None
    except OSError as exc:
        return False, exc.errno
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def va_drivers() -> list[str]:
    """Every VA-API driver installed in this container, by file name."""
    found: list[str] = []
    for directory in VA_DRIVER_DIRS:
        try:
            entries = sorted(p.name for p in directory.iterdir())
        except OSError:
            continue
        found += [name for name in entries if name.endswith("_drv_video.so")]
    return sorted(set(found))


def runtime_state() -> dict:
    """Is the userspace half of hardware encoding installed?

    Passing the render node through is only half the job, and it is the half
    that looks finished. ``/dev/dri/renderD128`` present and openable, and
    HandBrake still says ``encqsvInit: qsv is not available on the system`` —
    because Quick Sync does not talk to the kernel directly. It goes through a
    VA-API driver (``iHD_drv_video.so``) and a Media SDK / oneVPL runtime, and
    a minimal container image ships neither.

    That distinction is the whole point of this function. "The build has no
    QSV encoder" and "the QSV runtime is not installed" produce the same
    HandBrake error and have opposite fixes: one means give up the hardware
    and re-encode in software, the other means install two packages. Telling
    someone to abandon the GPU they just finished passing through, because a
    driver is missing, is the worst answer available.

    ``{"ok", "drivers": [...], "detail", "fix"}``.
    """
    drivers = va_drivers()
    state = {"ok": bool(drivers), "drivers": drivers, "detail": "", "fix": ""}
    if drivers:
        state["detail"] = "VA-API driver(s) installed: " + ", ".join(drivers) + "."
        return state
    state["detail"] = (
        "No VA-API driver is installed in this container, so the GPU cannot "
        "actually be used for encoding even though the render node is there. "
        "Quick Sync and VAAPI both reach the hardware through one of "
        + " or ".join(INTEL_VA_DRIVERS)
        + ", and a minimal container image ships neither."
    )
    state["fix"] = "Run on the Proxmox host: adr-doctor --fix {ctid}"
    return state


def describe() -> dict:
    """What hardware encoding this container can and cannot do.

    ``{"available", "nodes": [...], "runtime", "detail", "fix"}``. Never
    raises: this is called from a diagnostic page, which is the last thing
    that should fail.

    ``available`` means the render node is there and can be opened — the
    kernel half. ``runtime`` answers the userspace half separately, because
    they fail independently and have nothing to do with each other.
    """
    info: dict = {
        "available": False,
        "nodes": [],
        "runtime": runtime_state(),
        "detail": "",
        "fix": "",
    }

    if not DRI_DIR.exists():
        info["detail"] = (
            "/dev/dri does not exist in this container, so there is no GPU to "
            "encode with. Hardware presets — Quick Sync, NVENC, VAAPI — cannot "
            "work until one is passed through."
        )
        info["fix"] = "Run on the Proxmox host: adr-doctor --fix {ctid}"
        return info

    nodes = render_nodes()
    info["nodes"] = nodes
    if not nodes:
        info["detail"] = (
            "/dev/dri exists but holds no render node (renderD*). That is the "
            "device hardware encoding actually uses; a card node alone is not "
            "enough."
        )
        info["fix"] = "Run on the Proxmox host: adr-doctor --fix {ctid}"
        return info

    for node in nodes:
        ok, err = _openable(node)
        if ok:
            info["available"] = True
            runtime = info["runtime"]
            if runtime["ok"]:
                info["detail"] = f"{node} is present and can be opened."
            else:
                # The node being fine is not the headline when the driver on
                # top of it is missing — that is the thing still broken.
                info["detail"] = (
                    f"{node} is present and can be opened, but "
                    + runtime["detail"][0].lower() + runtime["detail"][1:]
                )
                info["fix"] = runtime["fix"]
            return info
        # Told apart because they need different fixes: a cgroup denial is a
        # host-side line, a permission problem is a group membership.
        if err == errno.EACCES:
            info["detail"] = (
                f"{node} is present but the service user may not open it "
                "(permission denied). The node is passed through; the user is "
                "not in the group that owns it."
            )
            # Deliberately not "usermod -aG render adr": the container's
            # 'render' group is unlikely to carry the same gid as the host's,
            # and the kernel checks the number, not the name. adr-doctor reads
            # the host's gid and joins the group that actually owns the node.
            info["fix"] = "Run on the Proxmox host: adr-doctor --fix {ctid}"
        else:
            info["detail"] = (
                f"{node} is present but cannot be opened "
                f"({errno.errorcode.get(err, err)}). The container's device "
                "cgroup is denying it."
            )
            info["fix"] = "Run on the Proxmox host: adr-doctor --fix {ctid}"
    return info


def preset_wants_hardware(preset_file: str, preset_name: str) -> str:
    """The hardware encoder a preset asks for, or "".

    Read from the preset file rather than guessed from its name: "Super HQ
    1080p30 Surround" says nothing about the encoder, and the encoder is the
    whole question.
    """
    if not preset_file or not os.path.isfile(preset_file):
        return ""
    try:
        import json

        with open(preset_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        logger.debug("Could not read preset file %s", preset_file, exc_info=True)
        return ""

    for preset in _walk_presets(data):
        if preset_name and preset.get("PresetName") != preset_name:
            continue
        encoder = str(preset.get("VideoEncoder", "")).lower()
        for marker in HARDWARE_ENCODERS:
            if marker in encoder:
                return encoder
    return ""


def _walk_presets(node):
    """Yield every preset object in a HandBrake preset file."""
    if isinstance(node, dict):
        if "PresetName" in node:
            yield node
        for value in node.values():
            yield from _walk_presets(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_presets(value)


def mentions_hardware(text: str) -> bool:
    """Whether HandBrake's output blames a hardware encoder."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in HARDWARE_ENCODERS) or (
        "quick sync" in lowered or "media sdk" in lowered
    )
