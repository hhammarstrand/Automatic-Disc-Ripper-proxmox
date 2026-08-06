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
import threading
import time
from pathlib import Path

from adr.ripper import SCAN_TIMEOUT, MakeMKVRipper

logger = logging.getLogger(__name__)

# Spawning goes through this name so a test can substitute the process without
# patching subprocess.Popen itself. Patching the stdlib module reaches every
# other module in the process — including type annotations evaluated by a later
# import, which fail with a baffling error a long way from the test.
_popen = subprocess.Popen

# <linux/cdrom.h>
CDROM_DRIVE_STATUS = 0x5326
CDS_NO_INFO = 0
CDS_DISC_OK = 4
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

# How long to let a MakeMKV scan run. Taken from adr.ripper, which is what a
# real rip uses: a diagnostic that gives up sooner than the operation it is
# diagnosing will fail discs that rip perfectly well, and send someone off to
# debug a drive that was never broken. Imported from adr.ripper rather than
# repeated here, because the two drifting apart is exactly the bug this comment
# warns about.

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
        #
        # "The drive did not give a usable status" is not "no disc". Some USB
        # enclosures answer CDS_NO_INFO or nothing at all, and this probe then
        # skipped both remaining steps and declared the drive healthy — while
        # the watcher, which falls back to the kernel's capacity, was ripping
        # discs from it. Two answers to one question. A read that then fails
        # with ENOMEDIUM is already reported properly.
        maybe_loaded = status_code in (None, CDS_NO_INFO, CDS_DISC_OK)
        if fd is not None and maybe_loaded:
            steps.append(_read_sector(fd, device))
        elif fd is not None:
            steps.append(_step(
                "Read from disc", "skip",
                "No disc loaded — insert one to test the read path.",
            ))

        if deep:
            steps.append(_makemkv_scan(device, has_disc=maybe_loaded))

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


def _makemkv_scan(device: str, has_disc: bool, timeout: int | None = None) -> dict:
    """Ask MakeMKV to open the disc — the only check that proves ripping works.

    It exercises the registration key, SG_IO and the read path in one go, which
    is exactly why it is slow and opt-in.

    The output is read as it arrives rather than waited for in one lump. That
    is what makes a timeout informative: a drive scanning title 7 of 34 and a
    drive that has said nothing at all are completely different problems, and
    only one is worth acting on. Waiting blind cannot tell them apart.
    """
    import shutil
    import threading

    # Read the module global at call time, not as a default bound at import.
    # A default argument is evaluated once, which makes the limit impossible to
    # change afterwards — including from a test.
    if timeout is None:
        timeout = SCAN_TIMEOUT

    binary = shutil.which("makemkvcon")
    if not binary:
        return _step("MakeMKV scan", "fail", "makemkvcon is not installed.")
    if not has_disc:
        return _step("MakeMKV scan", "skip", "No disc loaded — nothing to scan.")

    try:
        proc = _popen(
            [binary, "-r", "--cache=1", "info", f"dev:{device}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            # Its own session, so the whole tree can be killed on timeout.
            # Killing only the leader leaves a child holding stdout open, and
            # the reader thread then waits on an EOF that never comes.
            start_new_session=True,
        )
    except OSError as exc:
        return _step("MakeMKV scan", "fail", f"Could not run makemkvcon: {exc}")

    lines: list[str] = []

    def _read() -> None:
        try:
            for line in proc.stdout:          # type: ignore[union-attr]
                lines.append(line.rstrip())
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(timeout)

    # Whether the *reader* finished is the question, not proc.poll().
    #
    # The reader ends at EOF, which happens the moment makemkvcon closes its
    # stdout — a fraction of a second before the process is reaped. Asking
    # poll() right then gets None for a scan that finished perfectly well, and
    # the probe reported "still scanning after 300s" for a drive that had
    # answered. A short wait() settles it, and leaves no zombie either.
    if not reader.is_alive():
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            proc.wait(timeout=10)

    if reader.is_alive() or proc.poll() is None:
        from adr.utils import kill_process_tree

        # The tree, not the leader: a surviving child holds stdout open and
        # the reader thread never reaches EOF.
        with contextlib.suppress(OSError):
            kill_process_tree(proc)
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            proc.wait(timeout=10)
        reader.join(timeout=5)

        if lines:
            # It was working. Slow is not broken, and saying "failed" sends
            # someone off to debug a drive that is doing its job.
            progress = _last_progress(lines)
            return _step(
                "MakeMKV scan", "warn",
                f"Still scanning after {timeout}s, so the check was stopped — but "
                f"the drive is answering ({len(lines)} lines of output"
                f"{', last: ' + progress if progress else ''}). A Blu-ray with many "
                "playlists genuinely takes this long. Nothing here says the drive "
                "or the disc is faulty.",
            )
        return _step(
            "MakeMKV scan", "fail",
            f"MakeMKV produced no output at all in {timeout}s. The drive is not "
            "answering — not merely slow.",
        )

    output = "\n".join(lines)
    titles = output.count("TINFO:")
    if "registration key" in output.lower() or "app_KeyExpired" in output:
        return _step(
            "MakeMKV scan", "fail",
            "MakeMKV rejected its registration key. Refresh it under Settings.",
        )
    if proc.returncode != 0:
        last = [ln for ln in lines if ln.strip()]
        return _step(
            "MakeMKV scan", "fail",
            f"MakeMKV exited {proc.returncode}: {last[-1] if last else 'no output'}",
        )
    return _step(
        "MakeMKV scan", "ok",
        f"MakeMKV opened the disc and reported {titles} title attribute(s). "
        "Ripping from this drive will work.",
    )


def _last_progress(lines: list[str]) -> str:
    """The most recent thing MakeMKV said it was doing, for a timeout message."""
    for line in reversed(lines):
        if line.startswith("MSG:"):
            parsed = MakeMKVRipper.parse_message(line)
            if parsed and parsed[0]:
                return parsed[0][:120]
        if line.startswith(("PRGC:", "PRGT:")):
            # PRGC:code,id,"name" — the trailing quoted field names the step.
            parts = line.rsplit(",", 1)
            if len(parts) == 2:
                return parts[1].strip().strip('"')[:120]
    return ""

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


# ------------------------------------------------------------------ #
# Running a probe in the background
#
# The MakeMKV probe allows five minutes, because that is what a Blu-ray with
# many playlists legitimately needs. Holding an HTTP request open that long
# does not work: a phone browser gives up long before, and the only thing the
# page can then say is "Load failed" — which reads as a broken drive when the
# drive is fine and still working.
#
# So the request starts the probe and returns immediately, and the page asks
# how it is getting on. One probe per device at a time; asking again while one
# is running joins the one already in flight rather than starting a second,
# because two MakeMKV processes on one drive is how you make a working drive
# fail.
# ------------------------------------------------------------------ #

_probes: dict[str, dict] = {}
_probes_lock = threading.Lock()


def _public(state: dict) -> dict:
    """The part of a probe's state the API hands out."""
    return {
        "device": state["device"],
        "running": state["running"],
        "deep": state["deep"],
        "elapsed": round(time.monotonic() - state["started_at"], 1),
        "result": state["result"],
        "error": state["error"],
    }


def start_probe(device: str, deep: bool = False) -> dict:
    """Begin probing *device* in the background. Returns its current state."""
    with _probes_lock:
        existing = _probes.get(device)
        if existing and existing["running"]:
            logger.info("A probe of %s is already running; joining it", device)
            return _public(existing)
        state = {
            "device": device,
            "deep": deep,
            "running": True,
            "result": None,
            "error": None,
            "started_at": time.monotonic(),
        }
        _probes[device] = state

    def run() -> None:
        try:
            result = probe_drive(device, deep=deep)
        except Exception as exc:            # noqa: BLE001 - reported, not raised
            logger.exception("Probe of %s failed", device)
            with _probes_lock:
                state["running"] = False
                state["error"] = str(exc)
            return
        with _probes_lock:
            state["running"] = False
            state["result"] = result

    threading.Thread(target=run, daemon=True, name=f"drivetest-{device}").start()
    return _public(state)


def probe_status(device: str) -> dict | None:
    """The state of the last probe of *device*, or None if there has been none."""
    with _probes_lock:
        state = _probes.get(device)
        return _public(state) if state else None
