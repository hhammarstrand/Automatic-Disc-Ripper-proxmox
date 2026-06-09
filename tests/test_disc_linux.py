"""Tests for the Linux optical-drive detection layer (adr.disc)."""

import subprocess

from adr import disc

# ------------------------------------------------------------------ #
# _sr_devices
# ------------------------------------------------------------------ #

class TestSrDevices:
    def test_finds_sr_devices(self, tmp_path, monkeypatch):
        sys_block = tmp_path / "sys" / "block"
        for name in ("sr0", "sr1", "sda", "loop0"):
            (sys_block / name).mkdir(parents=True)
        monkeypatch.setattr(disc, "Path", _path_factory(tmp_path))
        # /dev/sr0 and /dev/sr1 must "exist" for them to be returned
        monkeypatch.setattr(disc.os.path, "exists", lambda p: p in ("/dev/sr0", "/dev/sr1"))
        assert disc._sr_devices() == ["/dev/sr0", "/dev/sr1"]

    def test_no_sys_block(self, monkeypatch):
        monkeypatch.setattr(disc.os.path, "exists", lambda p: False)
        # Point Path("/sys/block") at a non-existent dir
        import pathlib
        real = pathlib.Path
        monkeypatch.setattr(disc, "Path", lambda p="": real("/nonexistent-sys-block-xyz") if str(p) == "/sys/block" else real(p))
        assert disc._sr_devices() == []


def _path_factory(base):
    """Return a Path shim that redirects /sys/... under a temp dir."""
    import pathlib
    real = pathlib.Path

    def _factory(p=""):
        s = str(p)
        if s.startswith("/sys/"):
            return real(str(base) + s)
        return real(p)
    return _factory


# ------------------------------------------------------------------ #
# _has_media
# ------------------------------------------------------------------ #

class TestHasMedia:
    def test_capacity_positive_means_media(self, monkeypatch):
        monkeypatch.setattr(disc, "_device_capacity", lambda dev: 4324560)
        assert disc._has_media("/dev/sr0") is True

    def test_empty_tray(self, monkeypatch):
        monkeypatch.setattr(disc, "_device_capacity", lambda dev: 0)

        def _raise_enomedium(*a, **k):
            raise OSError(123, "No medium found")

        monkeypatch.setattr(disc.os, "open", _raise_enomedium)
        assert disc._has_media("/dev/sr0") is False

    def test_spinning_up_eio(self, monkeypatch):
        monkeypatch.setattr(disc, "_device_capacity", lambda dev: 0)

        def _raise_eio(*a, **k):
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(disc.os, "open", _raise_eio)
        assert disc._has_media("/dev/sr0") is True


# ------------------------------------------------------------------ #
# _blkid_label
# ------------------------------------------------------------------ #

class TestBlkidLabel:
    def test_returns_label(self, monkeypatch):
        monkeypatch.setattr(
            disc.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="THE_MATRIX\n", stderr=""),
        )
        assert disc._blkid_label("/dev/sr0") == "THE_MATRIX"

    def test_empty_label_is_none(self, monkeypatch):
        monkeypatch.setattr(
            disc.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 2, stdout="\n", stderr=""),
        )
        assert disc._blkid_label("/dev/sr0") is None

    def test_blkid_missing_is_none(self, monkeypatch):
        def _raise(*a, **k):
            raise FileNotFoundError("blkid")

        monkeypatch.setattr(disc.subprocess, "run", _raise)
        assert disc._blkid_label("/dev/sr0") is None


# ------------------------------------------------------------------ #
# eject_drive
# ------------------------------------------------------------------ #

class TestEjectDrive:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(disc.shutil, "which", lambda n: "/usr/bin/eject")
        monkeypatch.setattr(
            disc.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
        )
        assert disc.eject_drive("/dev/sr0") is True

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(disc.shutil, "which", lambda n: "/usr/bin/eject")
        monkeypatch.setattr(
            disc.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="not found"),
        )
        assert disc.eject_drive("/dev/sr0") is False

    def test_exception(self, monkeypatch):
        monkeypatch.setattr(disc.shutil, "which", lambda n: "/usr/bin/eject")

        def _raise(*a, **k):
            raise OSError("boom")

        monkeypatch.setattr(disc.subprocess, "run", _raise)
        assert disc.eject_drive("/dev/sr0") is False


# ------------------------------------------------------------------ #
# list_optical_drives
# ------------------------------------------------------------------ #

class TestListOpticalDrives:
    def test_assembles_entries(self, monkeypatch):
        monkeypatch.setattr(disc, "_sr_devices", lambda: ["/dev/sr0", "/dev/sr1"])
        monkeypatch.setattr(disc, "_has_media", lambda dev: dev == "/dev/sr0")
        monkeypatch.setattr(disc, "_blkid_label", lambda dev: "MOVIE" if dev == "/dev/sr0" else None)
        result = disc.list_optical_drives()
        assert result == [
            {"drive": "/dev/sr0", "volume_name": "MOVIE", "has_disc": True},
            {"drive": "/dev/sr1", "volume_name": None, "has_disc": False},
        ]


# ------------------------------------------------------------------ #
# DiscWatcher
# ------------------------------------------------------------------ #

class TestDiscWatcher:
    def test_register_drive_normalizes(self):
        w = disc.DiscWatcher(drives=["/dev/sr0"])
        w.register_drive("/dev/sr0")
        assert "/dev/sr0" in w._known_drives

    def test_resolve_drives_list_mode(self):
        w = disc.DiscWatcher(drives=["/dev/sr0", "/dev/sr1"])
        assert w._resolve_drives() == ["/dev/sr0", "/dev/sr1"]

    def test_fire_callbacks_invokes_listeners(self):
        w = disc.DiscWatcher(drives=["/dev/sr0"])
        seen = []
        w.on_disc_inserted(lambda drive, label: seen.append((drive, label)))
        w._fire_callbacks("/dev/sr0", "DISC")
        assert seen == [("/dev/sr0", "DISC")]

    def test_callback_exception_is_isolated(self):
        # A throwing callback must not break the watcher or other callbacks.
        w = disc.DiscWatcher(drives=["/dev/sr0"])
        seen = []
        w.on_disc_inserted(lambda d, lbl: (_ for _ in ()).throw(RuntimeError("boom")))
        w.on_disc_inserted(lambda d, lbl: seen.append(d))
        w._fire_callbacks("/dev/sr0", None)
        assert seen == ["/dev/sr0"]

    def test_new_drive_callback_fires(self):
        w = disc.DiscWatcher(drives="auto")
        seen = []
        w.on_new_drive(lambda drive: seen.append(drive))
        w._fire_new_drive_callbacks("/dev/sr2")
        assert seen == ["/dev/sr2"]
