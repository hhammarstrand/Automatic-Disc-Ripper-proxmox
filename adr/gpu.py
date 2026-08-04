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

#: Where shared libraries land, for the runtime that sits above the driver.
LIB_DIRS = (
    Path("/usr/lib/x86_64-linux-gnu"),
    Path("/usr/lib64"),
    Path("/usr/lib"),
)

#: The VA driver Quick Sync and VAAPI both go through. iHD covers Broadwell
#: and later, i965 the older parts; either one means the stack is installed.
INTEL_VA_DRIVERS = ("iHD_drv_video.so", "i965_drv_video.so")
AMD_VA_DRIVERS = ("radeonsi_drv_video.so", "r600_drv_video.so")
NVIDIA_VA_DRIVERS = ("nvidia_drv_video.so",)

#: PCI vendor ids, as /sys spells them.
VENDOR_INTEL = "0x8086"
VENDOR_AMD = "0x1002"
VENDOR_NVIDIA = "0x10de"

#: Which VA driver each vendor's hardware actually needs. A driver for
#: somebody else's GPU is not a substitute: a container with Mesa installed
#: has radeonsi_drv_video.so and cannot encode a frame on an Intel chip.
VENDOR_DRIVERS = {
    VENDOR_INTEL: INTEL_VA_DRIVERS,
    VENDOR_AMD: AMD_VA_DRIVERS,
    VENDOR_NVIDIA: NVIDIA_VA_DRIVERS,
}

#: Where the DRM class lives, for reading a node's PCI vendor.
DRM_CLASS_DIR = Path("/sys/class/drm")

#: Quick Sync needs a second thing above the VA driver, and it comes in two
#: parts that are easy to mistake for each other.
#:
#: The *dispatcher* is the library HandBrake links against. It implements no
#: encoding at all; its whole job is to find a runtime at load time and hand
#: over. libvpl.so is the oneVPL dispatcher, libmfx.so the old MSDK one.
QSV_DISPATCHER_LIBS = ("libvpl.so", "libmfx.so")

#: The *runtime* is what actually encodes: libmfx-gen for oneVPL on Gen11 and
#: later, libmfxhw64 for the older Media SDK. A container with the dispatcher
#: and no runtime has a loader with nothing to load, and HandBrake reports
#: exactly what a container with neither reports — "qsv is not available on
#: the system". Counting the dispatcher as sufficient is why that state was
#: once called installed.
QSV_RUNTIME_LIBS = ("libmfx-gen.so", "libmfxhw64.so")

#: How long to wait for vainfo. It either answers at once or the stack is
#: wedged, which is itself the answer.
VAINFO_TIMEOUT = 20


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


def gpu_vendor() -> str:
    """The PCI vendor id of the first render node, or "".

    Which driver is the right one depends entirely on whose GPU it is, and
    the node itself is the only thing that knows.
    """
    for node in render_nodes():
        path = DRM_CLASS_DIR / os.path.basename(node) / "device" / "vendor"
        try:
            return path.read_text().strip().lower()
        except OSError:
            continue
    return ""


def _libs_matching(prefixes: tuple) -> list[str]:
    """Installed libraries whose name starts with one of *prefixes*."""
    found: list[str] = []
    for directory in LIB_DIRS:
        try:
            entries = sorted(p.name for p in directory.iterdir())
        except OSError:
            continue
        found += [
            name for name in entries
            if any(name.startswith(prefix) for prefix in prefixes)
        ]
    return sorted(set(found))


def qsv_runtime_libs() -> list[str]:
    """The Media SDK / oneVPL runtimes installed — the part that encodes."""
    return _libs_matching(QSV_RUNTIME_LIBS)


def qsv_dispatcher_libs() -> list[str]:
    """The dispatchers installed — the part that only finds a runtime."""
    return _libs_matching(QSV_DISPATCHER_LIBS)


def runtime_state() -> dict:
    """Is the userspace half of hardware encoding installed?

    Passing the render node through is only half the job, and it is the half
    that looks finished. ``/dev/dri/renderD128`` present and openable, and
    HandBrake still says ``encqsvInit: qsv is not available on the system`` —
    because Quick Sync does not talk to the kernel directly. It goes through a
    VA-API driver (``iHD_drv_video.so``) and then a Media SDK / oneVPL
    runtime, and a minimal container image ships neither.

    That distinction is the whole point of this function. "The build has no
    QSV encoder" and "the QSV runtime is not installed" produce the same
    HandBrake error and have opposite fixes: one means give up the hardware
    and re-encode in software, the other means install two packages. Telling
    someone to abandon the GPU they just finished passing through, because a
    driver is missing, is the worst answer available.

    Both halves are checked *against the vendor of the actual GPU*. Asking
    only "is any VA driver installed?" is the same false green one level up:
    a container with Mesa has ``radeonsi_drv_video.so`` and still cannot
    encode a single frame on an Intel chip.

    ``{"ok", "vendor", "drivers", "libs", "missing", "detail", "fix"}``.
    """
    vendor = gpu_vendor()
    drivers = va_drivers()
    libs = qsv_runtime_libs()
    dispatchers = qsv_dispatcher_libs()
    state = {
        "ok": False, "vendor": vendor, "drivers": drivers, "libs": libs,
        "dispatchers": dispatchers, "missing": [], "detail": "", "fix": "",
    }

    wanted = VENDOR_DRIVERS.get(vendor, ())
    if wanted:
        have_driver = any(name in wanted for name in drivers)
        driver_names = " or ".join(wanted)
    else:
        # An unrecognised vendor: any VA driver is the best guess available,
        # and guessing quietly beats a confident wrong answer about hardware
        # this code has never heard of.
        have_driver = bool(drivers)
        driver_names = "a VA-API driver"

    if not have_driver:
        state["missing"].append(driver_names)

    # The runtime is Quick Sync's alone. VAAPI and NVENC do not load it, so
    # demanding it on an AMD box would invent a problem.
    if vendor == VENDOR_INTEL and not libs:
        state["missing"].append(" or ".join(QSV_RUNTIME_LIBS))

    if not state["missing"]:
        state["ok"] = True
        installed = ", ".join(drivers + libs) or "none found"
        state["detail"] = f"The GPU driver stack is installed: {installed}."
        return state

    detail = (
        "The driver stack this GPU needs is not installed in the container, so "
        "it cannot encode anything even though the render node is there. "
        f"Missing: {'; '.join(state['missing'])}."
    )
    if drivers and not have_driver:
        # Worth naming: it is why a looser check called this fine, and it is
        # what makes the situation confusing to look at from inside.
        detail += (
            f" There are VA-API drivers installed ({', '.join(drivers)}), but "
            "none of them drives this GPU."
        )
    if not libs and dispatchers:
        # The most confusing shape of this: `ls` shows libvpl.so.2 sitting
        # there, so the stack looks present. It is a loader with nothing to
        # load, and it fails exactly like having neither.
        detail += (
            f" {', '.join(dispatchers)} is installed, but that is only the "
            "dispatcher — it finds a runtime and hands over, and encodes "
            "nothing itself."
        )
    state["detail"] = detail
    state["fix"] = "Run on the Proxmox host: adr-doctor --fix {ctid}"
    return state


def vainfo() -> dict:
    """Ask the VA-API stack whether it actually works on this GPU.

    Everything above this function reasons from file names: the node is
    there, the driver is there, therefore it should work. ``vainfo`` does not
    reason — it opens the device, loads the driver and lists what the hardware
    will do, which is the difference between "the pieces are installed" and
    "encoding works". A driver too old for the chip, a chip with no encode
    engine, a render node that belongs to a different card: all of them look
    fine from a directory listing and all of them fail here.

    ``{"ran", "ok", "driver", "encoders": [...], "output"}``. Never raises.
    """
    import shutil
    import subprocess

    result = {"ran": False, "ok": False, "driver": "", "encoders": [], "output": ""}
    if not shutil.which("vainfo"):
        result["output"] = (
            "vainfo is not installed, so the driver stack could not be asked "
            "whether it works — only whether its files are present."
        )
        return result

    nodes = render_nodes()
    cmd = ["vainfo", "--display", "drm"]
    if nodes:
        cmd += ["--device", nodes[0]]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, errors="replace",
            timeout=VAINFO_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result["output"] = f"vainfo could not be run: {exc}"
        return result

    result["ran"] = True
    output = (proc.stdout or "") + (proc.stderr or "")
    result["output"] = output.strip()

    for line in output.splitlines():
        if "driver version" in line.lower():
            result["driver"] = line.split(":", 1)[-1].strip()
            break

    # An encode entrypoint is the thing that matters. A driver that loads and
    # offers decoding only cannot satisfy a hardware encode preset, and that
    # is a real configuration — some chips ship without the encode engine.
    result["encoders"] = sorted({
        line.split(":")[0].strip()
        for line in output.splitlines()
        if "VAEntrypointEncSlice" in line or "VAEntrypointEncPicture" in line
    })
    result["ok"] = proc.returncode == 0 and bool(result["encoders"])
    return result


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
