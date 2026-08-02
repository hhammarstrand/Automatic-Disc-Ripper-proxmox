"""Tests for the Storage and drive-health API endpoints.

These pin down two judgements the UI makes, both of which are easy to get
subtly wrong:

* keeping films on the container's own disk is a valid setup, not a warning —
  a banner that fires on a correct configuration teaches people to ignore
  banners;
* a disc reported by sysfs does not mean the container can open the drive,
  because inside an LXC /sys is the *host's* sysfs.
"""

import os

import pytest
import yaml

from adr.config import Config
from web.app import create_app


def _make_config(tmp_path, **overrides):
    """A Config whose paths all live under tmp_path."""
    data = {
        "raw_path": str(tmp_path / "raw"),
        "completed_path": str(tmp_path / "completed"),
        "staging_path": str(tmp_path / "staging"),
        "plex_path": "",
        "watch_path": "",
        "watch_output_path": "",
    }
    data.update(overrides)
    path = tmp_path / "adr.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return Config(path)


def _client(config):
    app = create_app(config)
    app.config["TESTING"] = True
    return app.test_client()


class TestStorageWarnings:
    def test_local_only_setup_is_not_a_warning(self, tmp_path):
        """No NAS configured: a plain local directory is exactly right."""
        config = _make_config(tmp_path, require_completed_mount=False)
        data = _client(config).get("/api/storage").get_json()

        assert data["warnings"] == [], (
            "a valid local-only install must not be flagged; "
            f"got {data['warnings']}"
        )
        assert data["paths"]["completed"]["is_mount"] is False
        assert data["require_mount"] is False

    def test_detached_share_is_a_warning(self, tmp_path):
        """require_completed_mount records 'the user attached storage'."""
        config = _make_config(tmp_path, require_completed_mount=True)
        data = _client(config).get("/api/storage").get_json()

        assert len(data["warnings"]) == 1
        message = data["warnings"][0]
        assert "not a mounted filesystem" in message
        assert "restart the container" in message.lower(), (
            "a bind-mount is captured at container start — the fix must be stated"
        )

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
    def test_unwritable_destination_is_always_a_warning(self, tmp_path):
        completed = tmp_path / "completed"
        completed.mkdir()
        completed.chmod(0o500)
        try:
            config = _make_config(tmp_path, require_completed_mount=False)
            data = _client(config).get("/api/storage").get_json()
            assert any("cannot write" in w for w in data["warnings"])
        finally:
            completed.chmod(0o700)

    def test_staging_is_reported_only_when_it_applies(self, tmp_path):
        """A local destination needs no staging — it would be a pointless copy."""
        config = _make_config(tmp_path, stage_locally=True)
        data = _client(config).get("/api/storage").get_json()
        assert data["staging"] is False
        assert "staging" not in data["paths"]


class TestDriveHealth:
    def test_problems_are_surfaced(self, tmp_path, monkeypatch):
        import adr.disc as disc

        monkeypatch.setattr(
            disc, "diagnose_passthrough",
            lambda: {"drives": [], "problems": ["the drive is missing"], "ok": False},
        )
        data = _client(_make_config(tmp_path)).get("/api/drives/health").get_json()
        assert data["ok"] is False
        assert data["problems"] == ["the drive is missing"]

    def test_healthy_reports_ok(self, tmp_path, monkeypatch):
        import adr.disc as disc

        monkeypatch.setattr(
            disc, "diagnose_passthrough",
            lambda: {"drives": [{"device": "/dev/sr0"}], "problems": [], "ok": True},
        )
        data = _client(_make_config(tmp_path)).get("/api/drives/health").get_json()
        assert data["ok"] is True
        assert data["problems"] == []
