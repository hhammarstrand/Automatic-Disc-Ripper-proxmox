"""Disc detection and ejection for Linux.

Uses the Linux kernel's SCSI/CD-ROM interfaces to:
  - Enumerate optical drives via /dev/sr* and /sys/block
  - Watch for disc insertion / removal events
  - Eject discs after ripping via CDROMEJECT ioctl
"""

import fcntl
import glob
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Linux ioctl codes from <linux/cdrom.h>
_CDROM_DRIVE_STATUS = 0x5326
_CDROM_EJECT = 0x5309
_CDS_NO_INFO = 0
_CDS_NO_DISC = 1
_CDS_TRAY_OPEN = 2
_CDS_DRIVE_NOT_READY = 3
_CDS_DISC_OK = 4


# ------------------------------------------------------------------ #
# Drive discovery
# ------------------------------------------------------------------ #

def _sysfs_name(device: str) -> str:
    """Return the /sys/block basename for a device path (e.g. '/dev/sr0' -> 'sr0')."""
    return os.path.basename(device)


def _read_volume_label(device: str) -> str | None:
    """Return the filesystem label of the disc in ``device`` or None."""
    try:
        result = subprocess.run(
            ["blkid", "-o", "value", "-s", "LABEL", device],
            capture_output=True, text=True, timeout=5,
        )
        label = result.stdout.strip()
        return label or None
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None


def _has_disc(device: str) -> bool:
    """Return True if a readable disc is present in ``device``.

    Uses the CDROM_DRIVE_STATUS ioctl so we don't need a mounted filesystem.
    """
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        status = fcntl.ioctl(fd, _CDROM_DRIVE_STATUS, 0)
    except OSError:
        return False
    finally:
        os.close(fd)
    return status == _CDS_DISC_OK


# ------------------------------------------------------------------ #
# Public API
# ------------------------------------------------------------------ #

def list_optical_drives() -> list[dict]:
    """Return info about all optical (CD/DVD/BD) drives on the system.

    Each entry: {"drive": "/dev/sr0", "volume_name": "MOVIE_TITLE" | None, "has_disc": bool}
    """
    drives = []
    for device in sorted(glob.glob("/dev/sr*")):
        has_disc = _has_disc(device)
        volume_name = _read_volume_label(device) if has_disc else None
        drives.append({
            "drive": device,
            "volume_name": volume_name,
            "has_disc": has_disc,
        })
    return drives


def get_drive_models() -> dict[str, str]:
    """Return a mapping of device path -> model string for optical drives.

    Reads /sys/block/<name>/device/{vendor,model}.
    Returns e.g. {"/dev/sr0": "HL-DT-ST BD-RE WH16NS40"}.
    """
    models: dict[str, str] = {}
    for device in sorted(glob.glob("/dev/sr*")):
        name = _sysfs_name(device)
        sysfs = Path("/sys/block") / name / "device"
        try:
            vendor = (sysfs / "vendor").read_text().strip()
        except OSError:
            vendor = ""
        try:
            model = (sysfs / "model").read_text().strip()
        except OSError:
            model = ""
        caption = " ".join(p for p in (vendor, model) if p) or "Unknown"
        models[device] = caption
    return models


# ------------------------------------------------------------------ #
# Disc ejection
# ------------------------------------------------------------------ #

def eject_drive(device: str) -> bool:
    """Eject a disc from the given device (e.g. '/dev/sr0').

    Tries the CDROMEJECT ioctl first, then falls back to the ``eject`` command.
    Returns True on success.
    """
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(fd, _CDROM_EJECT, 0)
            logger.info("Ejected disc from %s via ioctl", device)
            return True
        finally:
            os.close(fd)
    except OSError as exc:
        logger.warning("CDROMEJECT ioctl failed for %s: %s; falling back to eject(1)", device, exc)

    try:
        subprocess.run(["eject", device], check=True, timeout=30, capture_output=True)
        logger.info("Ejected disc from %s via eject(1)", device)
        return True
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        logger.exception("Failed to eject drive %s", device)
        return False


# ------------------------------------------------------------------ #
# Disc watcher (event-driven)
# ------------------------------------------------------------------ #

# Callback signature: callback(device: str, volume_name: str | None)
DiscInsertedCallback = Callable[[str, str | None], None]
# Callback for newly discovered drives: callback(device: str)
NewDriveCallback = Callable[[str], None]


class DiscWatcher:
    """Watches for disc insertion events across all monitored optical drives.

    Runs a background thread that polls the kernel's CD-ROM status for each
    drive.  When *new* device nodes appear at runtime (e.g. a USB DVD drive
    is plugged in) the ``on_new_drive`` callbacks are fired so that the
    pipeline manager can hot-add a DrivePipeline.
    """

    def __init__(
        self,
        drives: list[str] | str = "auto",
        poll_interval: float = 3.0,
    ):
        """
        Args:
            drives: List of device paths to monitor (e.g. ["/dev/sr0", "/dev/sr1"]) or "auto".
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
        # All devices we have ever seen (so we can detect new ones)
        self._known_drives: set[str] = set()
        # Cached drive list for auto mode (avoids re-globbing every poll)
        self._cached_drives: list[str] = []
        self._drives_cache_time: float = 0.0

    def on_disc_inserted(self, callback: DiscInsertedCallback) -> None:
        """Register a callback that fires when a disc is inserted."""
        self._callbacks.append(callback)

    def on_new_drive(self, callback: NewDriveCallback) -> None:
        """Register a callback that fires when a *new* optical drive appears."""
        self._new_drive_callbacks.append(callback)

    def register_drive(self, device: str) -> None:
        """Mark a device as known (e.g. added from outside)."""
        from adr.utils import normalize_drive
        self._known_drives.add(normalize_drive(device))

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
        """Determine which devices to monitor (cached 30s in auto mode)."""
        if isinstance(self._drives_config, list):
            return [d.rstrip("/") for d in self._drives_config]
        # "auto" – discover all optical drives (cache to reduce overhead)
        now = time.monotonic()
        if now - self._drives_cache_time > 30.0:
            self._cached_drives = [d["drive"] for d in list_optical_drives()]
            self._drives_cache_time = now
        return self._cached_drives

    def _watch_loop(self) -> None:
        """Polling-based disc detection loop using the CDROM_DRIVE_STATUS ioctl."""
        # Initial state snapshot — check for discs already inserted
        for drive in self._resolve_drives():
            self._known_drives.add(drive)
            has_disc = _has_disc(drive)
            volume_name = _read_volume_label(drive) if has_disc else None
            self._disc_present[drive] = has_disc

            if has_disc:
                logger.info("Disc already present in %s at startup: %s", drive, volume_name)
                self._fire_callbacks(drive, volume_name)

        logger.info(
            "DiscWatcher initial state: %s",
            {d: ("disc" if v else "empty") for d, v in self._disc_present.items()},
        )

        while not self._stop_event.is_set():
            time.sleep(self._poll_interval)
            try:
                drives = self._resolve_drives()

                # Detect newly appeared devices
                for drive in drives:
                    if drive not in self._known_drives:
                        logger.info("New optical drive detected: %s", drive)
                        self._known_drives.add(drive)
                        self._fire_new_drive_callbacks(drive)

                for drive in drives:
                    has_disc = _has_disc(drive)
                    was_present = self._disc_present.get(drive, False)

                    if has_disc and not was_present:
                        volume_name = _read_volume_label(drive)
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
