"""Tests for adr.storage — the Storage page's inspection helpers."""

import os

import pytest

from adr.storage import (
    NAS_PORTS,
    SERVICE_UID,
    build_nas_url,
    build_setup_command,
    describe_path,
    probe_nas,
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

    def test_smb_command_prompts_for_password_on_the_host(self):
        cmd = build_setup_command("smb", "nas", "media", 200, username="plex")
        assert "NAS_USERNAME=plex" in cmd
        # The real password must never be embedded — only a placeholder.
        assert "NAS_PASSWORD='<password>'" in cmd

    def test_missing_ctid_leaves_an_obvious_placeholder(self):
        assert "<CTID>" in build_setup_command("nfs", "h", "/s")

    def test_custom_mountpoint_included(self):
        cmd = build_setup_command("nfs", "h", "/s", 1, mountpoint="/mnt/films")
        assert "NAS_MOUNTPOINT=/mnt/films" in cmd


def test_service_uid_matches_the_installer():
    """SERVICE_UID is what users allow on their NAS export — it must not drift."""
    from pathlib import Path

    installer = Path(__file__).resolve().parent.parent / "scripts" / "install-container.sh"
    assert f"ADR_UID:-{SERVICE_UID}" in installer.read_text(encoding="utf-8")
