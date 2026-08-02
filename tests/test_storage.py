"""Tests for adr.storage — the Storage page's inspection helpers."""

import os

import pytest

from adr.storage import (
    NAS_PORTS,
    SERVICE_UID,
    build_nas_url,
    build_setup_command,
    check_destination,
    describe_path,
    probe_nas,
    should_stage,
)


class TestDescribePath:
    def test_root_is_a_mount_point(self):
        d = describe_path("/")
        assert d["exists"] is True
        assert d["is_mount"] is True
        assert d["fstype"]
        assert d["total_gb"] and d["total_gb"] > 0

    def test_missing_directory_is_reported_not_raised(self):
        d = describe_path("/definitely/not/here/12345")
        assert d["exists"] is False
        assert d["is_mount"] is False
        assert d["total_gb"] is None

    def test_plain_directory_is_not_a_mount(self, tmp_path):
        d = describe_path(tmp_path)
        assert d["exists"] is True
        assert d["is_mount"] is False, "a freshly created temp dir is not a mount point"
        assert d["is_network"] is False

    def test_writability_reflects_the_running_user(self, tmp_path):
        assert describe_path(tmp_path)["writable"] is True
        if os.getuid() != 0:  # root ignores the mode bits
            os.chmod(tmp_path, 0o500)
            assert describe_path(tmp_path)["writable"] is False

    def test_file_instead_of_directory(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        assert describe_path(f)["exists"] is False

    def test_accepts_str_and_path(self, tmp_path):
        assert describe_path(str(tmp_path))["path"] == describe_path(tmp_path)["path"]


class TestProbeNas:
    def test_rejects_unknown_protocol(self):
        r = probe_nas("ftp", "192.0.2.1")
        assert r["ok"] is False
        assert "nfs" in r["error"] and "smb" in r["error"]

    def test_rejects_empty_host(self):
        assert probe_nas("nfs", "")["ok"] is False

    def test_unresolvable_host_is_reported_clearly(self):
        r = probe_nas("nfs", "nas.invalid.example", timeout=2)
        assert r["ok"] is False
        assert "resolve" in r["error"].lower()

    def test_only_storage_ports_are_probed(self):
        """The endpoint must not be usable as a general port scanner."""
        assert set(NAS_PORTS) == {"nfs", "smb"}
        assert NAS_PORTS["nfs"] == 2049
        assert NAS_PORTS["smb"] == 445


class TestBuildNasUrl:
    @pytest.mark.parametrize(
        ("kind", "host", "share", "expected"),
        [
            ("nfs", "192.168.1.10", "/volume1/media", "nfs://192.168.1.10/volume1/media"),
            ("nfs", "192.168.1.10", "volume1/media", "nfs://192.168.1.10/volume1/media"),
            ("smb", "nas.local", "media", "smb://nas.local/media"),
            ("SMB", "nas.local", "/media/", "smb://nas.local/media"),
        ],
    )
    def test_normalises_slashes_and_case(self, kind, host, share, expected):
        assert build_nas_url(kind, host, share) == expected


class TestBuildSetupCommand:
    def test_nfs_command_has_no_credentials(self):
        cmd = build_setup_command("nfs", "192.168.1.10", "/volume1/media", 200)
        assert "NAS_URL=nfs://192.168.1.10/volume1/media" in cmd
        assert "adr-setup-nas 200" in cmd
        assert "PASSWORD" not in cmd

    def test_smb_without_password_prompts_on_the_host(self):
        cmd = build_setup_command("smb", "nas", "media", 200, username="plex")
        assert "NAS_USERNAME=plex" in cmd
        assert cmd.startswith("read -rsp"), "password should be read on the host"
        assert 'NAS_PASSWORD="$NAS_PASSWORD"' in cmd

    def test_smb_with_password_embeds_it(self):
        cmd = build_setup_command("smb", "nas", "media", 200, username="p", password="hunter2")
        assert "NAS_PASSWORD='hunter2'" in cmd
        assert not cmd.startswith("read -rsp"), "no prompt needed when supplied"

    @pytest.mark.parametrize(
        "password",
        ["it's", "a'b'c", "; rm -rf /", "$(whoami)", "`id`", 'quote"and\\slash'],
    )
    def test_password_is_shell_escaped(self, password):
        """A password with shell metacharacters must not become executable code."""
        import shlex

        cmd = build_setup_command("smb", "nas", "media", 1, username="u", password=password)
        line = next(ln for ln in cmd.splitlines() if "NAS_PASSWORD=" in ln)
        token = shlex.split(line.rstrip("\\").strip())[0]
        assert token == f"NAS_PASSWORD={password}", "password must survive quoting intact"

    def test_password_ignored_for_nfs(self):
        cmd = build_setup_command("nfs", "h", "/s", 1, password="secret")
        assert "secret" not in cmd

    def test_missing_ctid_leaves_an_obvious_placeholder(self):
        assert "<CTID>" in build_setup_command("nfs", "h", "/s")

    def test_custom_mountpoint_included(self):
        cmd = build_setup_command("nfs", "h", "/s", 1, mountpoint="/mnt/films")
        assert "NAS_MOUNTPOINT=/mnt/films" in cmd


class TestCheckDestination:
    def test_ok_for_a_normal_writable_directory(self, tmp_path):
        ok, msg = check_destination(tmp_path)
        assert ok is True
        assert msg == ""

    def test_missing_directory_is_rejected(self):
        ok, msg = check_destination("/nope/not/here")
        assert ok is False
        assert "does not exist" in msg

    def test_require_mount_rejects_a_plain_directory(self, tmp_path):
        """The NAS case: an unmounted share looks like an ordinary empty dir."""
        ok, msg = check_destination(tmp_path, require_mount=True)
        assert ok is False
        assert "not a mounted filesystem" in msg
        assert "restart the container" in msg

    def test_require_mount_accepts_a_real_mount_point(self):
        """A genuine mount point passes — provided we can also write to it.

        '/' is a mount point but is not writable by an ordinary user, and
        check_destination rightly rejects it for that reason. Use a tmpfs that
        is world-writable so this exercises the mount check rather than the
        permission check.
        """
        import os.path

        writable_mount = next(
            (p for p in ("/dev/shm", "/tmp", "/run/shm")
             if os.path.ismount(p) and os.access(p, os.W_OK | os.X_OK)),
            None,
        )
        if writable_mount is None:
            pytest.skip("no writable mount point available on this machine")

        ok, msg = check_destination(writable_mount, require_mount=True)
        assert ok is True, msg

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses permission bits")
    def test_unwritable_directory_is_rejected(self, tmp_path):
        os.chmod(tmp_path, 0o500)
        ok, msg = check_destination(tmp_path)
        assert ok is False
        assert "not writable" in msg
        assert str(SERVICE_UID) in msg


def test_service_uid_matches_the_installer():
    """SERVICE_UID is what users allow on their NAS export — it must not drift."""
    from pathlib import Path

    installer = Path(__file__).resolve().parent.parent / "scripts" / "install-container.sh"
    assert f"ADR_UID:-{SERVICE_UID}" in installer.read_text(encoding="utf-8")


class TestShouldStage:
    def test_local_destination_is_not_staged(self, tmp_path):
        """Staging to and from the same local disk would be a pointless copy."""
        assert should_stage(tmp_path, enabled=True) is False

    def test_disabled_never_stages(self, tmp_path):
        assert should_stage(tmp_path, enabled=False) is False

    def test_network_destination_is_staged(self, monkeypatch):
        import adr.storage as s
        monkeypatch.setattr(s, "describe_path", lambda p: {"is_network": True})
        assert s.should_stage("/mnt/nas", enabled=True) is True

    def test_network_destination_respects_the_switch(self, monkeypatch):
        import adr.storage as s
        monkeypatch.setattr(s, "describe_path", lambda p: {"is_network": True})
        assert s.should_stage("/mnt/nas", enabled=False) is False
