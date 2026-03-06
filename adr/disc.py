"""Disc detection and ejection for Windows.

Uses WMI (Windows Management Instrumentation) to:
  - Enumerate optical drives
  - Watch for disc insertion / removal events
  - Eject discs after ripping
"""

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


def _import_wmi():
    """Lazy-import wmi so the rest of the app can load on non-Windows for testing."""
    try:
        import pythoncom
        import wmi
        return wmi, pythoncom
    except ImportError:
        logger.error("wmi / pywin32 packages are required. Install with: pip install wmi pywin32")
        raise


# ------------------------------------------------------------------ #
# Drive discovery
# ------------------------------------------------------------------ #

def list_optical_drives() -> list[dict]:
    """Return info about all optical (CD/DVD) drives on the system.

    Each entry: {"drive": "D:", "volume_name": "MOVIE_TITLE" | None, "has_disc": bool}
    """
    wmi_mod, pythoncom = _import_wmi()
    pythoncom.CoInitialize()
    try:
        c = wmi_mod.WMI()
        drives = []
        for disk in c.Win32_LogicalDisk(DriveType=5):  # 5 = Compact disc
            drives.append({
                "drive": disk.DeviceID,
                "volume_name": disk.VolumeName or None,
                "has_disc": bool(disk.Size),
            })
        return drives
    finally:
        pythoncom.CoUninitialize()


def get_drive_models() -> dict[str, str]:
    """Return a mapping of drive letter -> model name for optical drives.

    Uses Win32_CDROMDrive to get the hardware model string.
    Returns e.g. {"D:": "HL-DT-ST BD-RE WH16NS40"}.
    """
    wmi_mod, pythoncom = _import_wmi()
    pythoncom.CoInitialize()
    try:
        c = wmi_mod.WMI()
        models = {}
        for cdrom in c.Win32_CDROMDrive():
            letter = (cdrom.Drive or "").rstrip("\\")
            if letter:
                models[letter] = cdrom.Caption or cdrom.Name or "Unknown"
        return models
    except Exception:
        # WMI / COM errors come in many forms (pywintypes.com_error,
        # wmi.x_wmi, x_wmi_uninitialised, etc.) — keep broad.
        logger.warning("Could not query drive models", exc_info=True)
        return {}
    finally:
        pythoncom.CoUninitialize()


# ------------------------------------------------------------------ #
# Disc ejection
# ------------------------------------------------------------------ #

def eject_drive(drive_letter: str) -> bool:
    """Eject a disc from the given drive (e.g. 'D:').

    Uses the Windows Shell COM object to invoke the Eject verb.
    Returns True on success.
    """
    import pythoncom
    try:
        pythoncom.CoInitialize()
        try:
            import win32com.client
            shell = win32com.client.Dispatch("Shell.Application")
            my_computer = shell.Namespace(17)  # ssfDRIVES
            drive_item = my_computer.ParseName(drive_letter + "\\")
            if drive_item is None:
                logger.error("Could not find drive %s for ejection", drive_letter)
                return False
            drive_item.InvokeVerb("Eject")
            logger.info("Ejected disc from drive %s", drive_letter)
            return True
        finally:
            pythoncom.CoUninitialize()
    except Exception:
        # COM / pywin32 errors come in many forms — keep broad.
        logger.exception("Failed to eject drive %s", drive_letter)
        return False


# ------------------------------------------------------------------ #
# Disc watcher (event-driven)
# ------------------------------------------------------------------ #

# Callback signature: callback(drive_letter: str, volume_name: str | None)
DiscInsertedCallback = Callable[[str, str | None], None]
# Callback for newly discovered drives: callback(drive_letter: str)
NewDriveCallback = Callable[[str], None]


class DiscWatcher:
    """Watches for disc insertion events across all monitored optical drives.

    Runs a background thread that polls WMI for optical drives and disc
    insertions.  When *new* drive letters appear at runtime (e.g. a USB
    DVD drive is plugged in, or Windows mounts an extra letter when a
    disc is inserted) the ``on_new_drive`` callbacks are fired so that
    the pipeline manager can hot-add a DrivePipeline.
    """

    def __init__(
        self,
        drives: list[str] | str = "auto",
        poll_interval: float = 3.0,
    ):
        """
        Args:
            drives: List of drive letters to monitor (e.g. ["D:", "E:"]) or "auto".
            poll_interval: Seconds between polling cycles (fallback if events fail).
        """
        self._drives_config = drives
        self._poll_interval = poll_interval
        self._callbacks: list[DiscInsertedCallback] = []
        self._new_drive_callbacks: list[NewDriveCallback] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Track which drives currently have a disc to avoid duplicate events
        self._disc_present: dict[str, bool] = {}
        # All drive letters we have ever seen (so we can detect new ones)
        self._known_drives: set[str] = set()
        # Cached drive list for auto mode (avoids WMI query every poll)
        self._cached_drives: list[str] = []
        self._drives_cache_time: float = 0.0

    def on_disc_inserted(self, callback: DiscInsertedCallback) -> None:
        """Register a callback that fires when a disc is inserted."""
        self._callbacks.append(callback)

    def on_new_drive(self, callback: NewDriveCallback) -> None:
        """Register a callback that fires when a *new* optical drive letter appears."""
        self._new_drive_callbacks.append(callback)

    def register_drive(self, drive_letter: str) -> None:
        """Mark a drive letter as known (e.g. added from outside)."""
        from adr.utils import normalize_drive
        self._known_drives.add(normalize_drive(drive_letter))

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
        """Determine which drive letters to monitor (cached 30s in auto mode)."""
        if isinstance(self._drives_config, list):
            return [d.rstrip("\\") for d in self._drives_config]
        # "auto" – discover all optical drives (cache to reduce WMI overhead)
        now = time.monotonic()
        if now - self._drives_cache_time > 30.0:
            self._cached_drives = [d["drive"] for d in list_optical_drives()]
            self._drives_cache_time = now
        return self._cached_drives

    def _watch_loop(self) -> None:
        """Polling-based disc detection loop.

        We use polling (checking Win32_LogicalDisk periodically) rather than
        WMI event subscriptions because the event approach requires a
        persistent COM apartment and is less reliable across Windows versions.
        """
        wmi_mod, pythoncom = _import_wmi()
        pythoncom.CoInitialize()
        try:
            c = wmi_mod.WMI()

            # Initial state snapshot — check for discs already inserted
            for drive in self._resolve_drives():
                self._known_drives.add(drive)
                disks = c.Win32_LogicalDisk(DeviceID=drive)
                has_disc = bool(disks and disks[0].Size)
                volume_name = disks[0].VolumeName if disks else None
                self._disc_present[drive] = has_disc

                # Fire callbacks for discs already present at startup
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

                    # Detect newly appeared drive letters
                    for drive in drives:
                        if drive not in self._known_drives:
                            logger.info("New optical drive detected: %s", drive)
                            self._known_drives.add(drive)
                            self._fire_new_drive_callbacks(drive)

                    for drive in drives:
                        disks = c.Win32_LogicalDisk(DeviceID=drive)
                        has_disc = bool(disks and disks[0].Size)
                        volume_name = disks[0].VolumeName if disks else None

                        was_present = self._disc_present.get(drive, False)

                        if has_disc and not was_present:
                            logger.info("Disc inserted in %s: %s", drive, volume_name)
                            self._fire_callbacks(drive, volume_name)

                        self._disc_present[drive] = has_disc
                except Exception:
                    logger.exception("Error in DiscWatcher poll cycle")
        finally:
            pythoncom.CoUninitialize()

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
