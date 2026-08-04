"""Tests for adr.drivetest — the active drive probe.

The probe's whole value is that each step answers a different question, and
that the first failure is the one worth reading. These tests pin down both:
that a failure stops the chain instead of cascading, and that the steps which
matter most — the cgroup denial and SG_IO — say what to do about it.
"""

import array
import contextlib
import errno
import os
import time

import pytest

from adr import drivetest


def _steps(result):
    return {s["name"]: s for s in result["steps"]}


@pytest.fixture
def openable(monkeypatch):
    """A drive whose node exists and opens cleanly.

    The fake open returns a *real* descriptor on /dev/null rather than a made-up
    number, so os.close is left alone. Stubbing os.close globally breaks
    subprocess.Popen — it needs it to close its pipe ends, and without that the
    child's output never reaches EOF and any reader blocks for ever. That cost
    an afternoon once; a real fd costs nothing.
    """
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    fd = os.open(os.devnull, os.O_RDONLY)
    monkeypatch.setattr(os, "open", lambda p, f: fd)
    yield
    with contextlib.suppress(OSError):
        os.close(fd)


def _ioctl(monkeypatch, drive_status=4, sg_ok=True):
    def fake(fd, request, arg=0, mutate=False):
        if request == drivetest.CDROM_DRIVE_STATUS:
            if isinstance(drive_status, OSError):
                raise drive_status
            return drive_status
        if request == drivetest.CDROM_DISC_STATUS:
            return 101  # data CD (mode 1)
        if request == drivetest.SG_GET_VERSION_NUM:
            if not sg_ok:
                raise OSError(errno.EINVAL, "Invalid argument")
            arg[0] = 30536
            return 0
        raise AssertionError(f"unexpected ioctl {request:#x}")
    monkeypatch.setattr(drivetest.fcntl, "ioctl", fake)


class TestMissingNode:
    def test_no_node_stops_the_chain(self, monkeypatch):
        monkeypatch.setattr(os.path, "exists", lambda p: False)
        result = drivetest.probe_drive("/dev/sr0")

        assert result["ok"] is False
        assert len(result["steps"]) == 1, "later steps are meaningless without a node"
        assert "adr-doctor" in result["steps"][0]["detail"]
        assert result["summary"] == result["steps"][0]["detail"]


class TestCgroupDenial:
    @pytest.mark.parametrize("err", [errno.EPERM, errno.EACCES])
    def test_denial_names_the_cgroup_rule(self, monkeypatch, err):
        monkeypatch.setattr(os.path, "exists", lambda p: True)

        def _deny(p, f):
            raise OSError(err, os.strerror(err))
        monkeypatch.setattr(os, "open", _deny)

        result = drivetest.probe_drive("/dev/sr0")
        assert result["ok"] is False
        detail = _steps(result)["Open device"]["detail"]
        assert "b 11:* rwm" in detail
        assert len(result["steps"]) == 2, "probing further would only echo the denial"

    @pytest.mark.parametrize("err", [errno.ENOMEDIUM, errno.ENXIO])
    def test_an_empty_tray_is_not_a_denial(self, monkeypatch, err):
        """The drive answered; there is just no disc in it."""
        monkeypatch.setattr(os.path, "exists", lambda p: True)

        def _empty(p, f):
            raise OSError(err, os.strerror(err))
        monkeypatch.setattr(os, "open", _empty)

        result = drivetest.probe_drive("/dev/sr0")
        assert result["ok"] is True
        assert _steps(result)["Open device"]["status"] == "ok"
        assert _steps(result)["Generic SCSI (SG_IO)"]["status"] == "skip"


class TestDriveStatus:
    @pytest.mark.parametrize("code,needle", [
        (1, "No disc"),
        (2, "tray is open"),
        (3, "busy or spinning up"),
        (4, "loaded and ready"),
    ])
    def test_each_tray_state_is_described(self, openable, monkeypatch, code, needle):
        _ioctl(monkeypatch, drive_status=code)
        monkeypatch.setattr(os, "pread", lambda *a: b"\x01CD001" + b"\x00" * 2042)
        result = drivetest.probe_drive("/dev/sr0")
        assert needle in _steps(result)["Drive status"]["detail"]

    def test_the_disc_type_is_named(self, openable, monkeypatch):
        """It is what makes the read step's outcome make sense."""
        _ioctl(monkeypatch, drive_status=4)
        monkeypatch.setattr(os, "pread", lambda *a: b"\x01CD001" + b"\x00" * 2042)
        detail = _steps(drivetest.probe_drive("/dev/sr0"))["Drive status"]["detail"]
        assert "data CD (mode 1)" in detail

    def test_an_unsupported_ioctl_is_a_warning_not_a_failure(self, openable, monkeypatch):
        """Some USB enclosures do not implement it. That is not a broken drive."""
        _ioctl(monkeypatch, drive_status=OSError(errno.ENOTTY, "Inappropriate ioctl"))
        result = drivetest.probe_drive("/dev/sr0")
        assert _steps(result)["Drive status"]["status"] == "warn"
        assert result["ok"] is True


class TestGenericScsi:
    def test_available_is_reported_with_the_version(self, openable, monkeypatch):
        _ioctl(monkeypatch)
        monkeypatch.setattr(os, "pread", lambda *a: b"\x01CD001" + b"\x00" * 2042)
        step = _steps(drivetest.probe_drive("/dev/sr0"))["Generic SCSI (SG_IO)"]
        assert step["status"] == "ok"
        assert "3.5.36" in step["detail"]
        assert "MakeMKV" in step["detail"]

    def test_missing_sg_fails_loudly_even_though_the_drive_opens(self, openable, monkeypatch):
        """The exact trap: everything looks healthy until a rip fails."""
        _ioctl(monkeypatch, sg_ok=False)
        monkeypatch.setattr(os, "pread", lambda *a: b"\x01CD001" + b"\x00" * 2042)
        result = drivetest.probe_drive("/dev/sr0")

        assert result["ok"] is False
        step = _steps(result)["Generic SCSI (SG_IO)"]
        assert "c 21:* rwm" in step["detail"]
        assert _steps(result)["Open device"]["status"] == "ok", (
            "the drive really does open — that is what makes this worth reporting"
        )


class TestRead:
    def test_a_filesystem_signature_is_named(self, openable, monkeypatch):
        _ioctl(monkeypatch)
        monkeypatch.setattr(os, "pread", lambda *a: b"\x01CD001\x01" + b"\x00" * 2040)
        step = _steps(drivetest.probe_drive("/dev/sr0"))["Read from disc"]
        assert step["status"] == "ok"
        assert "CD001" in step["detail"]

    def test_eio_is_a_warning_and_says_why(self, openable, monkeypatch):
        _ioctl(monkeypatch)

        def _eio(*a):
            raise OSError(errno.EIO, "Input/output error")
        monkeypatch.setattr(os, "pread", _eio)

        step = _steps(drivetest.probe_drive("/dev/sr0"))["Read from disc"]
        assert step["status"] == "warn"
        assert "audio CD" in step["detail"]

    def test_no_disc_skips_the_read(self, openable, monkeypatch):
        _ioctl(monkeypatch, drive_status=1)
        step = _steps(drivetest.probe_drive("/dev/sr0"))["Read from disc"]
        assert step["status"] == "skip"


class FakePopen:
    """Stands in for a makemkvcon process, streaming *lines* then exiting.

    ``linger`` keeps stdout open after the lines are exhausted, which is what a
    scan still working when the clock runs out looks like.
    """

    def __init__(self, lines, returncode=0, linger=False):
        self._lines = list(lines)
        self._linger = linger
        self.returncode = None
        self._final = returncode
        self.stdout = self
        self.killed = False

    def __iter__(self):
        yield from self._lines
        if self._linger:
            time.sleep(30)          # longer than any test's timeout
        self.returncode = self._final

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class TestMakeMkvScan:
    def _run(self, monkeypatch, stdout="", returncode=0, linger=False):
        lines = [ln + "\n" for ln in stdout.splitlines()]
        fake = FakePopen(lines, returncode, linger)
        # Narrow on purpose: patching subprocess.Popen itself reaches every
        # module in the process and breaks annotations on later imports.
        monkeypatch.setattr(drivetest, "_popen", lambda *a, **k: fake)
        import shutil
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/makemkvcon")
        return fake

    def test_a_successful_scan_counts_titles(self, openable, monkeypatch):
        _ioctl(monkeypatch)
        monkeypatch.setattr(os, "pread", lambda *a: b"\x01CD001" + b"\x00" * 2042)
        self._run(monkeypatch, stdout="TINFO:0,2,0,\"x\"\nTINFO:0,9,0,\"1:52:03\"\n")

        step = _steps(drivetest.probe_drive("/dev/sr0", deep=True))["MakeMKV scan"]
        assert step["status"] == "ok"
        assert "2 title" in step["detail"]

    def test_an_expired_key_is_named_as_such(self, openable, monkeypatch):
        _ioctl(monkeypatch)
        monkeypatch.setattr(os, "pread", lambda *a: b"\x01CD001" + b"\x00" * 2042)
        self._run(monkeypatch, stdout="This application version is too old, registration key expired\n",
                  returncode=1)

        step = _steps(drivetest.probe_drive("/dev/sr0", deep=True))["MakeMKV scan"]
        assert step["status"] == "fail"
        assert "Settings" in step["detail"], "the fix is one click away; say where"

    def test_a_slow_but_answering_drive_is_a_warning_not_a_failure(
        self, openable, monkeypatch,
    ):
        """The bug this replaced: a Blu-ray that simply takes a while was
        reported as a failure, sending the user off to debug a working drive.

        Output arriving at all is the evidence that separates the two cases.
        """
        _ioctl(monkeypatch)
        monkeypatch.setattr(os, "pread", lambda *a: b"\x01CD001" + b"\x00" * 2042)
        fake = self._run(
            monkeypatch,
            stdout='MSG:3007,0,2,"Scanning title 7"\nPRGC:5017,1,"Analyzing"',
            linger=True,
        )
        monkeypatch.setattr(drivetest, "SCAN_TIMEOUT", 1)

        step = _steps(
            drivetest.probe_drive("/dev/sr0", deep=True))["MakeMKV scan"]
        assert step["status"] == "warn", "answering-but-slow is not a fault"
        assert "drive is answering" in step["detail"]
        assert "Analyzing" in step["detail"] or "Scanning title 7" in step["detail"]
        assert fake.killed, "the scan must be stopped, not left running"

    def test_a_silent_drive_is_still_a_failure(self, openable, monkeypatch):
        """No output at all is the case that really is broken."""
        _ioctl(monkeypatch)
        monkeypatch.setattr(os, "pread", lambda *a: b"\x01CD001" + b"\x00" * 2042)
        self._run(monkeypatch, stdout="", linger=True)
        monkeypatch.setattr(drivetest, "SCAN_TIMEOUT", 1)

        step = _steps(drivetest.probe_drive("/dev/sr0", deep=True))["MakeMKV scan"]
        assert step["status"] == "fail"
        assert "no output at all" in step["detail"]
        assert "not merely slow" in step["detail"]

    def test_the_limit_is_not_stricter_than_a_real_rip(self):
        """A diagnostic that gives up sooner than the operation it diagnoses
        will fail discs that rip perfectly well."""
        import inspect

        import adr.ripper as ripper_module

        # One constant, shared, rather than two that can drift apart.
        assert drivetest.SCAN_TIMEOUT is ripper_module.SCAN_TIMEOUT
        assert ripper_module.SCAN_TIMEOUT >= 300
        source = inspect.getsource(ripper_module.MakeMKVRipper._run_scan)
        assert "timeout=SCAN_TIMEOUT" in source, "the scan stopped using it"

    def test_no_disc_skips_the_scan(self, openable, monkeypatch):
        _ioctl(monkeypatch, drive_status=1)
        import shutil
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/makemkvcon")
        step = _steps(drivetest.probe_drive("/dev/sr0", deep=True))["MakeMKV scan"]
        assert step["status"] == "skip"

    def test_the_scan_is_not_run_unless_asked(self, openable, monkeypatch):
        """It takes up to 90 seconds; it must never happen by accident."""
        _ioctl(monkeypatch)
        monkeypatch.setattr(os, "pread", lambda *a: b"\x01CD001" + b"\x00" * 2042)

        def _explode(*a, **k):
            raise AssertionError("makemkvcon must not run without deep=True")
        monkeypatch.setattr(drivetest.subprocess, "run", _explode)

        assert "MakeMKV scan" not in _steps(drivetest.probe_drive("/dev/sr0"))


class TestRescan:
    def test_a_host_only_drive_is_separated_from_having_none(self, monkeypatch):
        """Scanning cannot help here, and saying so is the useful part."""
        monkeypatch.setattr("adr.disc._sr_devices", lambda: [])
        monkeypatch.setattr("adr.disc.diagnose_passthrough", lambda: {
            "drives": [{"device": "/dev/sr0", "node_present": False}],
            "problems": ["not present in this container"], "ok": False,
        })
        result = drivetest.rescan_drives()
        assert result["count"] == 0
        assert result["host_only"] == ["/dev/sr0"]

    def test_a_working_drive_is_not_listed_as_host_only(self, monkeypatch):
        monkeypatch.setattr("adr.disc._sr_devices", lambda: ["/dev/sr0"])
        monkeypatch.setattr("adr.disc.diagnose_passthrough", lambda: {
            "drives": [{"device": "/dev/sr0", "node_present": True}],
            "problems": [], "ok": True,
        })
        result = drivetest.rescan_drives()
        assert result["count"] == 1
        assert result["host_only"] == []


def test_the_sg_buffer_is_mutable():
    """fcntl.ioctl needs a writable buffer to return the version into."""
    buf = array.array("i", [0])
    buf[0] = 30536
    assert buf[0] == 30536
