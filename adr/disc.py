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


def _has_media(device: str) -> bool:
    """True if a disc is currently loaded in the drive.

    Primary signal: kernel capacity (/sys/block/<name>/size > 0) which covers
    data discs, audio CDs and label-less Blu-rays. Falls back to a non-blocking
    open() so we still work if sysfs is unavailable inside a minimal container.
    """
    if _device_capacity(device) > 0:
        return True
    # Fallback: try to open the raw device without blocking. An empty tray
    # raises ENOMEDIUM / ENXIO; a loaded disc opens (or returns EIO/EBUSY
    # mid-spinup, which we also treat as "media present").
    fd = None
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        return True
    except OSError as exc:
        return exc.errno in (5, 16)  # EIO, EBUSY → spinning up → media present
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _blkid_label(device: str) -> str | None:
    """Return the volume label of a device via blkid, or None.

    Returns None for audio CDs and label-less discs; callers fall back to the
    disc-label parser, so this is best-effort only.
    """
    try:
        result = subprocess.run(
            ["blkid", "-s", "LABEL", "-o", "value", device],
            capture_output=True, text=True, timeout=5,
        )
        label = result.stdout.strip()
        return label or None
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        logger.debug("blkid failed for %s", device, exc_info=True)
        return None


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
