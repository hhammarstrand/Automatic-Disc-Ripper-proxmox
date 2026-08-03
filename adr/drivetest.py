"""Actively test an optical drive, rather than inferring that it works.

Everything else in this application observes the drive passively — sysfs says a
disc is loaded, the node exists, the open() succeeded. That is enough to run on,
but it is not enough to answer "is my drive actually working?", which is the
question people have at the point where nothing is happening and they cannot
tell whose fault it is.

So this pokes the drive and reports each step separately. The steps are ordered
so the first failure is the informative one:

    node → open → drive status → generic SCSI → read

Each answers something different. A node that opens but rejects SG_IO means
MakeMKV will fail while the dashboard looks healthy, because MakeMKV drives the
disc through SG_IO and nothing else here does. A drive that reports a disc but
cannot read a sector means the cgroup allows open() but not reads.
"""

import array
import contextlib
import errno
import fcntl
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# <linux/cdrom.h>
CDROM_DRIVE_STATUS = 0x5326
CDROM_DISC_STATUS = 0x5327
CDSL_CURRENT = 0x7FFFFFFF

_DRIVE_STATUS = {
    0: ("no_info", "The drive did not report a tray status."),
    1: ("no_disc", "No disc in the tray."),
    2: ("tray_open", "The tray is open."),
    3: ("not_ready", "The drive is busy or spinning up."),
    4: ("disc_ok", "A disc is loaded and ready."),
}

_DISC_TYPE = {
    0: "no information",
    100: "audio CD",
    101: "data CD (mode 1)",
    102: "data CD (mode 2 form 1)",
    103: "data CD (mode 2 form 2)",
    104: "XA CD",
    105: "mixed-mode CD",
}

# <scsi/sg.h> — MakeMKV talks to the drive through this interface, so its
# presence is the single most predictive thing we can cheaply check.
SG_GET_VERSION_NUM = 0x2282

# Sector 16 is where ISO 9660 and UDF put their volume descriptor, so it is
# both a real read and one that means something if it succeeds.
_READ_OFFSET = 16 * 2048
_READ_SIZE = 2048


def _step(name: str, status: str, detail: str) -> dict:
    """One probe result. *status* is ok | warn | fail | skip."""
    return {"name": name, "status": status, "detail": detail}


def _errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno, str(exc.errno))


def probe_drive(device: str, deep: bool = False) -> dict:
    """Run the probes against *device* and report every step.

    With *deep*, MakeMKV is asked to scan the disc as well — the only check that
    exercises the registration key and the full read path, and the only one slow
    enough to need saying so.

    Returns ``{"device", "steps": [...], "ok": bool, "summary": str}``.
    """
    steps: list[dict] = []
    fd = None

    try:
        # 1. Is the node even here?
        if not os.path.exists(device):
            steps.append(_step(
                "Device node", "fail",
                f"{device} does not exist in this container. The passthrough did "
                "not apply — restart the container, or run adr-doctor --fix on the host.",
            ))
            return _finish(device, steps)
        steps.append(_step("Device node", "ok", f"{device} is present."))

        # 2. Can we open it? This is where a cgroup denial shows up.
        try:
            fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
            steps.append(_step("Open device", "ok", "The device opened successfully."))
        except OSError as exc:
            if exc.errno in (errno.ENOMEDIUM, errno.ENXIO):
                # Reachable, just empty. Opening an empty tray failing this way
                # is normal and says nothing bad about the drive.
                steps.append(_step(
                    "Open device", "ok",
                    "The drive answered (no disc loaded).",
                ))
            else:
                steps.append(_step(
                    "Open device", "fail",
                    f"Cannot open {device} ({_errno_name(exc)}: {exc.strerror}). "
                    "The container's device cgroup is denying access — the config "
                    "needs 'lxc.cgroup2.devices.allow: b 11:* rwm'.",
                ))
                return _finish(device, steps)

        # 3. Tray and media state, straight from the drive.
        status_code = None
        if fd is not None:
            try:
                status_code = fcntl.ioctl(fd, CDROM_DRIVE_STATUS, CDSL_CURRENT)
                key, text = _DRIVE_STATUS.get(status_code, ("unknown", f"Status {status_code}."))
                # Naming the disc type is what makes the read step below
                # readable: an audio CD has no data sectors, so a warning there
                # is expected rather than a fault.
                if status_code == 4:
                    with contextlib.suppress(OSError):
                        disc = fcntl.ioctl(fd, CDROM_DISC_STATUS, CDSL_CURRENT)
                        if disc in _DISC_TYPE:
                            text += f" Type: {_DISC_TYPE[disc]}."
                steps.append(_step(
                    "Drive status", "ok" if key != "no_info" else "warn", text,
                ))
            except OSError as exc:
                steps.append(_step(
                    "Drive status", "warn",
                    f"The drive did not answer the status ioctl ({_errno_name(exc)}). "
                    "Some USB enclosures do not implement it; this alone is not fatal.",
                ))
        else:
            steps.append(_step("Drive status", "ok", "No disc in the tray."))

        # 4. The interface MakeMKV actually uses.
        if fd is not None:
            steps.append(_check_sg(fd, device))
        else:
            steps.append(_step(
                "Generic SCSI (SG_IO)", "skip",
                "Needs an open device — insert a disc and test again.",
            ))

        # 5. A real read. Proves the cgroup allows more than open().
        if fd is not None and status_code == 4:
            steps.append(_read_sector(fd, device))
        elif fd is not None:
            steps.append(_step(
                "Read from disc", "skip",
                "No disc loaded — insert one to test the read path.",
            ))

        if deep:
            steps.append(_makemkv_scan(device, has_disc=(status_code == 4)))

        return _finish(device, steps)
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _check_sg(fd: int, device: str = "") -> dict:
    """Ask for the SG interface version — cheap, and MakeMKV depends on it."""
    buf = array.array("i", [0])
    try:
        fcntl.ioctl(fd, SG_GET_VERSION_NUM, buf, True)
    except OSError as exc:
        # Name the actual node rather than "the drive's /dev/sg node": it is
        # resolved from sysfs, and it is what has to appear in the container
        # config.
        node = sg_node_for(device) if device else None
        which = f"'{node}'" if node else "the drive's /dev/sg node"
        return _step(
            "Generic SCSI (SG_IO)", "fail",
            f"The SCSI generic interface is not available ({_errno_name(exc)}). "
            "MakeMKV drives the disc through it, so ripping will fail even though "
            f"the drive otherwise looks fine. The container must pass through "
            f"{which} and allow 'c 21:* rwm'. On the Proxmox host: adr-doctor --fix <CTID>",
        )
    version = buf[0]
    return _step(
        "Generic SCSI (SG_IO)", "ok",
        f"Available (sg version {version // 10000}.{version // 100 % 100}.{version % 100}) "
        "— this is the interface MakeMKV uses.",
    )


def _read_sector(fd: int, device: str) -> dict:
    """Read the volume descriptor. An audio CD has none, which is not a fault."""
    try:
        data = os.pread(fd, _READ_SIZE, _READ_OFFSET)
    except OSError as exc:
        if exc.errno == errno.EIO:
            return _step(
                "Read from disc", "warn",
                "The sector could not be read. Normal for an audio CD; otherwise "
                "the disc may be dirty, damaged, or still spinning up.",
            )
        return _step(
            "Read from disc", "fail",
            f"Reading from {device} failed ({_errno_name(exc)}: {exc.strerror}).",
        )

    if not data:
        return _step("Read from disc", "warn", "The read returned no data.")

    # ISO 9660 stamps "CD001" and UDF "BEA01"/"NSR0x" at the start of the
    # descriptor; naming it turns a byte count into something meaningful.
    marker = ""
    for tag in (b"CD001", b"BEA01", b"NSR02", b"NSR03"):
        if tag in data[:16]:
            marker = tag.decode()
            break
    return _step(
        "Read from disc", "ok",
        f"Read {len(data)} bytes from the disc"
        + (f" — filesystem signature '{marker}'." if marker else "."),
    )


def _makemkv_scan(device: str, has_disc: bool, timeout: int = 90) -> dict:
    """Ask MakeMKV to open the disc — the only check that proves ripping works.

    It exercises the registration key, SG_IO and the read path in one go, which
    is exactly why it is slow and opt-in.
    """
    import shutil

    binary = shutil.which("makemkvcon")
    if not binary:
        return _step("MakeMKV scan", "fail", "makemkvcon is not installed.")
    if not has_disc:
        return _step("MakeMKV scan", "skip", "No disc loaded — nothing to scan.")

    try:
        result = subprocess.run(
            [binary, "-r", "--cache=1", "info", f"dev:{device}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _step(
            "MakeMKV scan", "fail",
            f"MakeMKV did not finish within {timeout}s. A scratched disc can take "
            "this long; a drive that never answers looks the same.",
        )
    except OSError as exc:
        return _step("MakeMKV scan", "fail", f"Could not run makemkvcon: {exc}")

    output = f"{result.stdout}\n{result.stderr}"
    titles = output.count("TINFO:")
    if "registration key" in output.lower() or "app_KeyExpired" in output:
        return _step(
            "MakeMKV scan", "fail",
            "MakeMKV rejected its registration key. Refresh it under Settings.",
        )
    if result.returncode != 0:
        last = [ln for ln in output.splitlines() if ln.strip()]
        return _step(
            "MakeMKV scan", "fail",
            f"MakeMKV exited {result.returncode}: {last[-1] if last else 'no output'}",
        )
    return _step(
        "MakeMKV scan", "ok",
        f"MakeMKV opened the disc and reported {titles} title attribute(s). "
        "Ripping from this drive will work.",
    )


def _finish(device: str, steps: list[dict]) -> dict:
    failed = [s for s in steps if s["status"] == "fail"]
    ok = not failed
    # The first failure is the informative one — the steps are ordered so that
    # everything after it is a consequence, not a separate problem.
    summary = "The drive answered every probe." if ok else failed[0]["detail"]
    return {"device": device, "steps": steps, "ok": ok, "summary": summary}


def rescan_drives() -> dict:
    """Re-read sysfs for optical drives, picking up anything hot-plugged.

    The watcher caches its drive list for 30 s in auto mode, so a drive plugged
    in a moment ago is invisible until that expires. This forces the issue
    instead of asking the user to wait without telling them why.
    """
    from adr.disc import _sr_devices, diagnose_passthrough

    devices = _sr_devices()
    health = diagnose_passthrough()
    return {
        "devices": devices,
        "count": len(devices),
        "problems": health["problems"],
        # Drives the host has that this container did not get — the difference
        # between "no drive" and "a drive you cannot reach".
        "host_only": [d["device"] for d in health["drives"] if not d["node_present"]],
    }


def sg_node_for(device: str) -> str | None:
    """The /dev/sg node belonging to *device*, from sysfs.

    Resolved per drive on purpose. The host's SATA disks have sg nodes too, and
    they are none of this container's business.
    """
    sg_dir = Path("/sys/block") / Path(device).name / "device" / "scsi_generic"
    try:
        for entry in sorted(sg_dir.iterdir()):
            return f"/dev/{entry.name}"
    except OSError:
        return None
    return None
