"""Disc detection and ejection for Linux.

Replaces the Windows WMI/COM implementation. Enumerates optical drives via
/sys/block (sr* devices), detects media presence and volume labels, and ejects
discs with the `eject` command.

The public interface (list_optical_drives, get_drive_models, eject_drive and the
DiscWatcher class) is kept identical to the original Windows module so the rest
of the application — pipeline.py, web/app.py — works unchanged. The only visible
difference is that "drive" values are device paths like "/dev/sr0" instead of
Windows drive letters like "D:".
"""

import contextlib
import errno
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Callback signatures (unchanged from the Windows version so pipeline/web work as-is)
# callback(drive: str, volume_name: str | None)
DiscInsertedCallback = Callable[[str, "str | None"], None]
# callback(drive: str)
NewDriveCallback = Callable[[str], None]


# ------------------------------------------------------------------ #
# Drive discovery
# ------------------------------------------------------------------ #

def _sr_devices() -> list[str]:
    """Return all /dev/sr* optical devices present on this host, sorted."""
    devices: list[str] = []
    sys_block = Path("/sys/block")
    if not sys_block.exists():
        return devices
    try:
        children = sorted(sys_block.iterdir(), key=lambda p: p.name)
    except OSError:
        logger.debug("Could not list /sys/block", exc_info=True)
        return devices
    for child in children:
        # sr0, sr1, ... are SCSI optical (CD/DVD/Blu-ray) devices
        if child.name.startswith("sr") and child.name[2:].isdigit():
            dev = f"/dev/{child.name}"
            if os.path.exists(dev):
                devices.append(dev)
    return devices


def _device_capacity(device: str) -> int:
    """Return the kernel-reported capacity of a drive in 512-byte sectors.

    Reads /sys/block/<name>/size. A value > 0 means media is loaded; an empty
    tray reports 0. This is more reliable than blkid alone, which misses audio
    CDs and some UDF/Blu-ray discs that lack a recognised filesystem label.
    """
    name = Path(device).name
    size_file = Path("/sys/block") / name / "size"
    try:
        return int(size_file.read_text().strip())
    except (OSError, ValueError):
        return 0


# Errnos a *reachable* drive can legitimately return from a non-blocking open:
# an empty tray, or a disc still spinning up. None of them mean "no access".
_EMPTY_TRAY_ERRNOS = frozenset({errno.ENOMEDIUM, errno.ENXIO})
_SPINNING_UP_ERRNOS = frozenset({errno.EIO, errno.EBUSY})

# Log a cgroup denial once per device instead of every three seconds.
_denied_devices: set[str] = set()


# <linux/cdrom.h>. The drive's own answer to "is there a disc in you", which
# is the only authoritative one — everything else here is inference.
CDROM_DRIVE_STATUS = 0x5326
CDSL_CURRENT = 0x7FFFFFFF
CDS_NO_INFO = 0
CDS_NO_DISC = 1
CDS_TRAY_OPEN = 2
CDS_DRIVE_NOT_READY = 3
CDS_DISC_OK = 4


def _drive_status(fd: int) -> int | None:
    """What the drive says about its tray, or None if it will not say.

    Some USB enclosures do not implement the ioctl at all, which is why the
    caller still has fallbacks. On a normal SATA drive this is exact.
    """
    import fcntl

    try:
        return int(fcntl.ioctl(fd, CDROM_DRIVE_STATUS, CDSL_CURRENT))
    except (OSError, ValueError):
        return None


def _probe_device(device: str) -> tuple[bool, int | None, int | None]:
    """Try to open *device* without blocking, and ask what is in it.

    Returns ``(reachable, errno, status)``. *reachable* is False only when the
    kernel refused us access to the device itself — inside an LXC that means
    the container's device cgroup is denying it, which is a configuration
    problem and not a disc that happens to be absent. An empty tray or a drive
    still spinning up is reachable; *errno* says which.

    *status* is the CDS_* constant above, and it is the point of this function.
    O_NONBLOCK on an optical drive is specified to succeed with an empty tray —
    that is how you are meant to open one in order to query it — so a
    successful open says nothing whatsoever about whether a disc is loaded.
    Reading it as "media present" is what made the service start ripping an
    empty drive the moment the container came up.
    """
    fd = None
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        return True, None, _drive_status(fd)
    except OSError as exc:
        if exc.errno in _EMPTY_TRAY_ERRNOS or exc.errno in _SPINNING_UP_ERRNOS:
            return True, exc.errno, None
        return False, exc.errno, None
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _has_media(device: str) -> bool:
    """True if a disc is currently loaded in the drive *and we can read it*.

    The ordering here is deliberate. Inside an LXC, /sys is the HOST's sysfs,
    so /sys/block/<name>/size reports the disc in the host's drive whether or
    not this container is allowed to touch it. Trusting that alone means the
    dashboard shows a disc, a rip job starts, and MakeMKV fails forty seconds
    later with something unhelpful. So access is confirmed first; only then is
    the kernel capacity used, which is what catches audio CDs and label-less
    Blu-rays that blkid cannot see.
    """
    reachable, err, status = _probe_device(device)
    if not reachable:
        if device not in _denied_devices:
            _denied_devices.add(device)
            if err == errno.ENOENT:
                logger.error(
                    "%s does not exist in this container — the device passthrough did "
                    "not apply at start. Run 'adr-doctor --fix <CTID>' on the Proxmox "
                    "host, or restart the container.", device,
                )
            else:
                logger.error(
                    "%s cannot be opened (%s) — the container's device cgroup is "
                    "denying access, so discs in it are ignored. Run "
                    "'adr-doctor --fix <CTID>' on the Proxmox host.",
                    device, errno.errorcode.get(err, err),
                )
        return False
    _denied_devices.discard(device)

    # The drive's own answer, where it gave one. It outranks everything below:
    # sysfs is the host's and can be stale, and a successful non-blocking open
    # means only that the device node works.
    if status in (CDS_NO_DISC, CDS_TRAY_OPEN):
        return False
    if status == CDS_DISC_OK:
        return True

    if err in _SPINNING_UP_ERRNOS:
        return True          # the drive is busy with a disc it is spinning up
    if status == CDS_DRIVE_NOT_READY:
        return False         # still waking up; the next poll will say properly
    # Nothing authoritative left. sysfs is the host's, so a positive capacity
    # is at least evidence that a disc is in this physical drive — and access
    # to it has already been confirmed above.
    return _device_capacity(device) > 0


#: States where starting a job would certainly fail, and waiting will not
#: change that. "not_ready" is deliberately absent: a drive spinning up a disc
#: someone has just pushed in reports it, and dropping that event would lose
#: the insertion entirely — the watcher fires on the *transition*, so there is
#: no second chance.
NOTHING_TO_RIP = frozenset({"missing", "denied", "empty", "tray_open"})


def media_status(device: str, display: str | None = None) -> dict:
    """Whether *device* has a disc ready to rip, and if not, why.

    Returns ``{"ready", "state", "detail"}``. *detail* is written to be shown
    to a person as it is: the previous answer to "there is no disc in the
    drive" was MakeMKV failing forty seconds later with an exit code, and an
    exit code is not an explanation.

    The states are distinct because the thing to do about them is distinct.
    An empty tray needs a disc; a missing device node needs the passthrough
    fixed on the host; a drive still spinning up needs ten seconds.

    *display* is the name its owner gave the drive, when the caller has a
    config to ask. Callers that are producing diagnostics — the Doctor page,
    preflight, adr-doctor — leave it out and get the device node, which is
    what you type into ``pct exec``.
    """
    # What to call the drive in these sentences. They are read on the
    # dashboard, under a card already headed "Internal", so the node alone
    # contradicts the thing above it. The two states whose fix is an
    # adr-doctor command on the host keep the node as well, because that
    # command names it — see Config.drive_display_full.
    shown = display or device
    both = f"{shown} ({device})" if shown != device else device

    if not os.path.exists(device):
        return {
            "ready": False, "state": "missing",
            "detail": (
                f"There is no {both} in this container. The drive was not "
                "passed through when the container started — run "
                "'adr-doctor --fix <CTID>' on the Proxmox host, or restart the "
                "container."
            ),
        }

    reachable, err, status = _probe_device(device)
    if not reachable:
        name = errno.errorcode.get(err, err)
        return {
            "ready": False, "state": "denied",
            "detail": (
                f"{both} exists but this container is not allowed to open it "
                f"({name}). The device cgroup is denying access — run "
                "'adr-doctor --fix <CTID>' on the Proxmox host."
            ),
        }

    if status == CDS_TRAY_OPEN:
        return {
            "ready": False, "state": "tray_open",
            "detail": f"The tray of {shown} is open. Close it with a disc in it.",
        }
    if status == CDS_NO_DISC:
        return {
            "ready": False, "state": "empty",
            "detail": f"There is no disc in {shown}. Put one in and try again.",
        }
    if status == CDS_DRIVE_NOT_READY or err in _SPINNING_UP_ERRNOS:
        return {
            "ready": False, "state": "not_ready",
            "detail": (
                f"{shown} is still reading the disc. Give it a few seconds and "
                "try again."
            ),
        }

    if _has_media(device):
        return {"ready": True, "state": "ready", "detail": f"A disc is loaded in {shown}."}

    return {
        "ready": False, "state": "empty",
        "detail": (
            f"No readable disc in {shown}. The drive answered but reported no "
            "media — if a disc is loaded, it may be one this drive cannot read."
        ),
    }


#: Devices whose blkid call has already timed out, and when.
#:
#: blkid on an optical drive that is busy — mid-rip, or spinning up — blocks
#: for the whole timeout and answers nothing. The dashboard polls every few
#: seconds, so that is five seconds of a blocked worker and a full traceback
#: in the log, over and over, for a drive that is working perfectly. Once it
#: has timed out, stop asking for a while.
_BLKID_BACKOFF_SECONDS = 120
_blkid_timed_out: dict[str, float] = {}


def _blkid_label(device: str) -> str | None:
    """Return the volume label of a device via blkid, or None.

    Returns None for audio CDs and label-less discs; callers fall back to the
    disc-label parser, so this is best-effort only.
    """
    last = _blkid_timed_out.get(device)
    if last is not None and (time.monotonic() - last) < _BLKID_BACKOFF_SECONDS:
        return None

    try:
        result = subprocess.run(
            ["blkid", "-s", "LABEL", "-o", "value", device],
            capture_output=True, text=True, timeout=5,
        )
        label = result.stdout.strip()
    except subprocess.TimeoutExpired:
        # Logged without the traceback: it says nothing a reader does not
        # already know from the message, and this happens on a loop.
        _blkid_timed_out[device] = time.monotonic()
        logger.debug(
            "blkid timed out on %s (busy drive); not asking again for %ds",
            device, _BLKID_BACKOFF_SECONDS,
        )
        return None
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        logger.debug("blkid failed for %s", device, exc_info=True)
        return None

    _blkid_timed_out.pop(device, None)
    return label or None


def _drive_model(device: str) -> str:
    """Return the vendor/model string for an sr device from sysfs."""
    base = Path("/sys/block") / Path(device).name / "device"
    try:
        vendor = (base / "vendor").read_text().strip() if (base / "vendor").exists() else ""
        model = (base / "model").read_text().strip() if (base / "model").exists() else ""
        full = f"{vendor} {model}".strip()
        return full or "Unknown"
    except OSError:
        return "Unknown"


def list_optical_drives() -> list[dict]:
    """Return info about all optical (CD/DVD/Blu-ray) drives on the system.

    Each entry: {"drive": "/dev/sr0", "volume_name": "MOVIE" | None, "has_disc": bool}
    """
    drives: list[dict] = []
    for dev in _sr_devices():
        present = _has_media(dev)
        label = _blkid_label(dev) if present else None
        drives.append({"drive": dev, "volume_name": label, "has_disc": present})
    return drives


def diagnose_passthrough() -> dict:
    """Explain why optical drives are or are not usable in this container.

    Inside an LXC, /sys is the HOST's sysfs, so /sys/block/sr* lists the host's
    optical drives whether or not the device was passed through. /dev/sr* only
    exists if the passthrough actually applied at container start. Comparing the
    two separates the two failure modes that otherwise look identical:

      * a drive the host has but the container cannot see — the bind-mount did
        not apply (typically the container autostarted before udev created the
        node), and only a container restart will fix it;
      * a node that exists but cannot be opened — the device cgroup is denying
        access, so the UI happily shows a disc while MakeMKV gets EPERM.

    Returns {"drives": [...], "problems": [...], "ok": bool}.
    """
    drives: list[dict] = []
    problems: list[str] = []

    sys_block = Path("/sys/block")
    host_names: list[str] = []
    if sys_block.exists():
        with contextlib.suppress(OSError):
            host_names = sorted(
                p.name for p in sys_block.iterdir()
                if p.name.startswith("sr") and p.name[2:].isdigit()
            )

    for name in host_names:
        dev = f"/dev/{name}"
        entry: dict = {
            "device": dev,
            "model": _drive_model(dev),
            "node_present": os.path.exists(dev),
            "openable": False,
            "error": None,
            "has_media": _device_capacity(dev) > 0,
        }
        if entry["node_present"]:
            # Same probe the watcher uses, so a clean report here means the
            # watcher will also act on discs in this drive.
            reachable, err, _status = _probe_device(dev)
            entry["openable"] = reachable
            if not reachable:
                entry["error"] = f"{errno.errorcode.get(err, err)}: {os.strerror(err)}"
        drives.append(entry)

        if not entry["node_present"]:
            problems.append(
                f"The host has {dev} ({entry['model']}) but it is not present in this "
                f"container. The device passthrough did not apply at start — this "
                f"usually happens when the container autostarts before the drive is "
                f"ready. Restart the container on the Proxmox host: pct reboot <CTID>"
            )
        elif not entry["openable"]:
            problems.append(
                f"{dev} exists but cannot be opened ({entry['error']}). The container's "
                f"device cgroup is denying access. On the Proxmox host, check that "
                f"/etc/pve/lxc/<CTID>.conf contains 'lxc.cgroup2.devices.allow: b 11:* rwm' "
                f"(block, not char), then restart the container."
            )

    if not host_names:
        problems.append(
            "No optical drive is visible even on the Proxmox host (nothing matching "
            "/sys/block/sr*). Check the drive is connected and powered."
        )

    return {"drives": drives, "problems": problems, "ok": not problems}


def get_drive_models() -> dict[str, str]:
    """Return a mapping of device path -> hardware model string.

    e.g. {"/dev/sr0": "HL-DT-ST BD-RE WH16NS40"}.
    """
    try:
        return {dev: _drive_model(dev) for dev in _sr_devices()}
    except Exception:
        logger.warning("Could not query drive models", exc_info=True)
        return {}


# ------------------------------------------------------------------ #
# Disc ejection
# ------------------------------------------------------------------ #

# <linux/cdrom.h>. Opening the tray is a single ioctl on the device, which is
# the whole of what this application needs — the disc is never mounted, because
# MakeMKV reads it raw.
CDROMEJECT = 0x5309
CDROM_LOCKDOOR = 0x5329

#: What the kernel says this drive can physically do. Worth asking before
#: blaming software for a tray that never moves: a slot loader and a caddy
#: drive both accept CDROMEJECT and neither has a tray to open.
CDROM_GET_CAPABILITY = 0x5331
_CAPABILITIES = (
    (0x1, "open tray"),
    (0x2, "close tray"),
    (0x4, "lock door"),
    (0x8, "select speed"),
    (0x20, "media changed"),
    (0x100, "play audio"),
)


def eject_capability(device: str) -> dict:
    """What the drive says about ejecting. ``{"ok", "can_eject", "detail"}``.

    Read-only: it asks the kernel what the drive is capable of, and never
    ejects anything. A diagnostic that opened the tray as a side effect would
    be a surprising thing to run while a disc is being read.
    """
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        return {
            "ok": False, "can_eject": None,
            "detail": f"could not open it ({errno.errorcode.get(exc.errno, exc.errno)})",
        }
    import fcntl

    try:
        flags = fcntl.ioctl(fd, CDROM_GET_CAPABILITY, 0)
    except OSError as exc:
        return {
            "ok": False, "can_eject": None,
            "detail": (
                "the drive does not answer CDROM_GET_CAPABILITY "
                f"({errno.errorcode.get(exc.errno, exc.errno)})"
            ),
        }
    finally:
        os.close(fd)

    if flags < 0:
        return {"ok": False, "can_eject": None, "detail": "the kernel returned no capabilities"}

    named = [name for bit, name in _CAPABILITIES if flags & bit]
    return {
        "ok": True,
        "can_eject": bool(flags & 0x1),
        "detail": ", ".join(named) or "nothing it will admit to",
    }


def _eject_ioctl(drive: str) -> str:
    """Ask the kernel directly. Returns "" on success, else why it failed.

    This is what the `eject` command does at the end of its work, minus the
    part that needs udev. In an LXC container there is no udev, so `eject`
    stops at "udev: not found mountpoint or device with the given name" and
    never reaches the ioctl — the tray stays shut and every rip since has
    needed the disc taken out by hand.
    """
    import fcntl

    fd = None
    try:
        fd = os.open(drive, os.O_RDONLY | os.O_NONBLOCK)
        # A previous reader may have locked the door; unlocking is advisory
        # and a drive that refuses it can still often eject.
        with contextlib.suppress(OSError):
            fcntl.ioctl(fd, CDROM_LOCKDOOR, 0)
        fcntl.ioctl(fd, CDROMEJECT)
        return ""
    except OSError as exc:
        return f"{errno.errorcode.get(exc.errno, exc.errno)}: {exc.strerror}"
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def forget_blkid_backoff(device: str) -> None:
    """Drop the "blkid timed out here" note for *device*.

    The backoff is armed when blkid blocks on a busy drive — mid-rip, or
    spinning up — and it was keyed on elapsed time alone. So a disc ejected
    and a new one put in ten seconds later inherited the previous disc's
    silence: the label came back None, and the job was named from the disc
    that was no longer in the drive. A disc change means the condition that
    armed it is over, whatever the clock says.
    """
    _blkid_timed_out.pop(device, None)


def eject_drive(drive: str) -> bool:
    """Eject the disc in the given drive (e.g. "/dev/sr0").

    The kernel first, the `eject` command second. That order looks backwards
    for a shell-first codebase, but this one runs in a container: `eject`
    consults udev before it does anything, and there is no udev in an LXC, so
    it fails on a drive that opens and ejects perfectly well. The command stays
    as the fallback because it knows how to unmount, which matters on a host
    where someone has mounted the disc.
    """
    reason = _eject_ioctl(drive)
    if not reason:
        logger.info("Ejected disc from drive %s", drive)
        forget_blkid_backoff(drive)
        return True
    logger.debug("Eject ioctl on %s failed (%s) — trying the eject command", drive, reason)

    eject_bin = shutil.which("eject") or "/usr/bin/eject"
    try:
        result = subprocess.run(
            [eject_bin, drive], capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("Ejected disc from drive %s", drive)
            forget_blkid_backoff(drive)
            return True
        logger.error(
            "eject failed for %s: %s (the kernel refused it too: %s)", drive,
            result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}",
            reason,
        )
        return False
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        logger.error(
            "Could not eject %s: the kernel refused it (%s) and the eject "
            "command could not be run", drive, reason, exc_info=True,
        )
        return False


# ------------------------------------------------------------------ #
# Disc watcher (polling-based)
# ------------------------------------------------------------------ #

class DiscWatcher:
    """Watches for disc insertion events across all monitored optical drives.

    Runs a background thread that polls sysfs for optical drives and disc
    insertions. When *new* device paths appear at runtime (e.g. a USB DVD drive
    is hot-plugged) the ``on_new_drive`` callbacks fire so the pipeline manager
    can hot-add a DrivePipeline.

    The public API mirrors the Windows implementation 1:1.
    """

    def __init__(
        self,
        drives: list[str] | str = "auto",
        poll_interval: float = 3.0,
    ):
        """
        Args:
            drives: List of device paths to monitor (e.g. ["/dev/sr0"]) or "auto".
            poll_interval: Seconds between polling cycles.
        """
        self._drives_config = drives
        self._poll_interval = poll_interval
        self._callbacks: list[DiscInsertedCallback] = []
        self._new_drive_callbacks: list[NewDriveCallback] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Track which drives currently have a disc to avoid duplicate events
        self._disc_present: dict[str, bool] = {}
        # All device paths we have ever seen (so we can detect new ones)
        self._known_drives: set[str] = set()
        # Cached drive list for auto mode (avoids scanning sysfs every poll)
        self._cached_drives: list[str] = []
        self._drives_cache_time: float = 0.0

    def on_disc_inserted(self, callback: DiscInsertedCallback) -> None:
        """Register a callback that fires when a disc is inserted."""
        self._callbacks.append(callback)

    def on_new_drive(self, callback: NewDriveCallback) -> None:
        """Register a callback that fires when a *new* optical drive appears."""
        self._new_drive_callbacks.append(callback)

    def register_drive(self, drive: str) -> None:
        """Mark a device path as known (e.g. added from outside)."""
        from adr.utils import normalize_drive
        self._known_drives.add(normalize_drive(drive))

    def start(self) -> None:
        """Start watching for disc events in a background thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("DiscWatcher is already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._watch_loop, daemon=True, name="DiscWatcher")
        self._thread.start()
        logger.info("DiscWatcher started (drives=%s)", self._drives_config)

    def stop(self) -> None:
        """Signal the watcher thread to stop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("DiscWatcher stopped")

    # -------------------------------------------------------------- #
    # Internal
    # -------------------------------------------------------------- #

    def refresh_drives(self) -> list[str]:
        """Drop the auto-mode cache so the next poll re-reads sysfs.

        Without this, a drive plugged in a moment ago stays invisible for up to
        30 seconds and nothing says why. The 'Rescan' button in the web UI calls
        this so the answer it gives is the current one.
        """
        self._drives_cache_time = 0.0
        drives = self._resolve_drives()
        logger.info("Drive list refreshed on request: %s", drives or "none")
        return drives

    def _resolve_drives(self) -> list[str]:
        """Determine which device paths to monitor (cached 30s in auto mode)."""
        if isinstance(self._drives_config, list):
            return list(self._drives_config)
        # "auto" — discover all optical drives (cache to reduce sysfs scans)
        now = time.monotonic()
        if now - self._drives_cache_time > 30.0:
            self._cached_drives = [d["drive"] for d in list_optical_drives()]
            self._drives_cache_time = now
        return self._cached_drives

    def _watch_loop(self) -> None:
        """Polling-based disc detection loop."""
        # Initial state snapshot — check for discs already inserted at startup
        for drive in self._resolve_drives():
            self._known_drives.add(drive)
            has_disc = _has_media(drive)
            volume_name = _blkid_label(drive) if has_disc else None
            self._disc_present[drive] = has_disc
            if has_disc:
                logger.info("Disc already present in %s at startup: %s", drive, volume_name)
                self._fire_callbacks(drive, volume_name)

        logger.info(
            "DiscWatcher initial state: %s",
            {d: ("disc" if v else "empty") for d, v in self._disc_present.items()},
        )

        # A watcher with nothing to watch would otherwise sit there silently for
        # ever. Say why: from inside the container "no drive" and "the drive was
        # not passed through" look the same, and only one of them is fixable.
        if not self._disc_present:
            try:
                for problem in diagnose_passthrough()["problems"]:
                    logger.error("Optical drive unavailable: %s", problem)
            except Exception:
                logger.exception("Could not diagnose optical passthrough")

        while not self._stop_event.is_set():
            self._stop_event.wait(self._poll_interval)
            if self._stop_event.is_set():
                break
            try:
                drives = self._resolve_drives()

                # Detect newly appeared device paths (hot-plugged drives)
                for drive in drives:
                    if drive not in self._known_drives:
                        logger.info("New optical drive detected: %s", drive)
                        self._known_drives.add(drive)
                        self._fire_new_drive_callbacks(drive)

                for drive in drives:
                    has_disc = _has_media(drive)
                    was_present = self._disc_present.get(drive, False)
                    if has_disc != was_present:
                        # The disc changed, so whatever made blkid block is
                        # over. Asking again now is the whole point.
                        forget_blkid_backoff(drive)
                    volume_name = _blkid_label(drive) if has_disc else None

                    if has_disc and not was_present:
                        logger.info("Disc inserted in %s: %s", drive, volume_name)
                        self._fire_callbacks(drive, volume_name)

                    self._disc_present[drive] = has_disc
            except Exception:
                logger.exception("Error in DiscWatcher poll cycle")

    def _fire_callbacks(self, drive: str, volume_name: str | None) -> None:
        for cb in self._callbacks:
            try:
                cb(drive, volume_name)
            except Exception:
                logger.exception("Error in disc-inserted callback")

    def _fire_new_drive_callbacks(self, drive: str) -> None:
        for cb in self._new_drive_callbacks:
            try:
                cb(drive)
            except Exception:
                logger.exception("Error in new-drive callback for %s", drive)
