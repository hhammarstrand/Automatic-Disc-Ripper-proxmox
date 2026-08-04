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


def _probe_device(device: str) -> tuple[bool, int | None]:
    """Try to open *device* without blocking.

    Returns ``(reachable, errno)``. *reachable* is False only when the kernel
    refused us access to the device itself — inside an LXC that means the
    container's device cgroup is denying it, which is a configuration problem
    and not a disc that happens to be absent. An empty tray or a drive still
    spinning up is reachable; *errno* says which.
    """
    fd = None
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        return True, None
    except OSError as exc:
        if exc.errno in _EMPTY_TRAY_ERRNOS or exc.errno in _SPINNING_UP_ERRNOS:
            return True, exc.errno
        return False, exc.errno
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
    reachable, err = _probe_device(device)
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

    if err in _SPINNING_UP_ERRNOS:
        return True          # the drive is busy with a disc it is spinning up
    if _device_capacity(device) > 0:
        return True
    return err is None       # open() succeeded outright → media present


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
            reachable, err = _probe_device(dev)
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

def eject_drive(drive: str) -> bool:
    """Eject the disc in the given drive (e.g. "/dev/sr0").

    Uses the `eject` command. Returns True on success.
    """
    eject_bin = shutil.which("eject") or "/usr/bin/eject"
    try:
        result = subprocess.run(
            [eject_bin, drive], capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("Ejected disc from drive %s", drive)
            return True
        logger.error(
            "eject failed for %s: %s", drive,
            result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}",
        )
        return False
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        logger.exception("Failed to eject drive %s", drive)
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
                    volume_name = _blkid_label(drive) if has_disc else None
                    was_present = self._disc_present.get(drive, False)

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
