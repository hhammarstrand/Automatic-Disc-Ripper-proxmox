"""Tests for adr.diagnostics — the in-app half of adr-doctor.

The value of a diagnostic page is entirely in whether it points at the real
problem. These tests pin down that each check says something the user can act
on, and that a check blowing up does not take the rest of the page with it.
"""

import types

import pytest

from adr import diagnostics


def _config(tmp_path, **overrides):
    for name in ("raw", "completed", "staging"):
        (tmp_path / name).mkdir(exist_ok=True)
    data = {
        "raw_path": tmp_path / "raw",
        "completed_path": tmp_path / "completed",
        "staging_path": tmp_path / "staging",
        "plex_path": "",
        "auto_move_to_plex": True,
        "require_completed_mount": False,
    }
    data.update(overrides)
    return types.SimpleNamespace(**data)


class TestDrives:
    def test_healthy_drives_pass(self, monkeypatch):
        monkeypatch.setattr(diagnostics, "diagnose_passthrough", lambda: {
            "drives": [{"device": "/dev/sr0", "openable": True}],
            "problems": [], "ok": True,
        })
        check = diagnostics.check_drives()
        assert check["status"] == "ok"
        assert "/dev/sr0" in check["detail"]

    def test_a_broken_drive_points_at_the_host(self, monkeypatch):
        """The container cannot repair its own passthrough; say where to go."""
        monkeypatch.setattr(diagnostics, "diagnose_passthrough", lambda: {
            "drives": [], "problems": ["the node is missing"], "ok": False,
        })
        check = diagnostics.check_drives()
        assert check["status"] == "fail"
        assert "the node is missing" in check["detail"]
        assert "adr-doctor" in check["fix"]


class TestTools:
    def test_both_present(self, monkeypatch):
        monkeypatch.setattr(diagnostics.shutil, "which", lambda n: f"/usr/bin/{n}")
        assert diagnostics.check_tools()["status"] == "ok"

    def test_a_missing_tool_is_named(self, monkeypatch):
        monkeypatch.setattr(
            diagnostics.shutil, "which",
            lambda n: None if n == "makemkvcon" else f"/usr/bin/{n}",
        )
        check = diagnostics.check_tools()
        assert check["status"] == "fail"
        assert "makemkvcon" in check["detail"]
        assert "HandBrakeCLI" not in check["detail"], "only the missing one is the problem"


class TestMakeMkvKey:
    def test_key_present(self, monkeypatch):
        import adr.makemkv_key as mk
        monkeypatch.setattr(mk, "read_existing_key", lambda: "T-" + "x" * 64)
        assert diagnostics.check_makemkv_key()["status"] == "ok"

    def test_no_key_is_a_failure_with_a_route_to_the_fix(self, monkeypatch):
        import adr.makemkv_key as mk
        monkeypatch.setattr(mk, "read_existing_key", lambda: None)
        check = diagnostics.check_makemkv_key()
        assert check["status"] == "fail"
        assert "Settings" in check["fix"]

    def test_the_key_itself_is_never_echoed(self, monkeypatch):
        """A registration key is a secret; the page must not leak it."""
        import adr.makemkv_key as mk
        secret = "T-" + "s" * 64
        monkeypatch.setattr(mk, "read_existing_key", lambda: secret)
        check = diagnostics.check_makemkv_key()
        assert secret not in check["detail"]


class TestDestination:
    def test_local_completed_path_passes(self, tmp_path):
        check = diagnostics.check_destination_path(_config(tmp_path))
        assert check["status"] == "ok"
        assert "Completed folder" in check["title"]

    def test_plex_library_is_the_destination_when_auto_move_is_on(self, tmp_path):
        plex = tmp_path / "plex"
        plex.mkdir()
        check = diagnostics.check_destination_path(
            _config(tmp_path, plex_path=str(plex), auto_move_to_plex=True)
        )
        assert "Plex library" in check["title"]
        assert str(plex) in check["detail"]

    def test_a_missing_destination_fails(self, tmp_path):
        check = diagnostics.check_destination_path(
            _config(tmp_path, completed_path=tmp_path / "gone")
        )
        assert check["status"] == "fail"


class TestScratch:
    def test_writable_scratch_passes(self, tmp_path):
        assert diagnostics.check_scratch(_config(tmp_path))["status"] in ("ok", "warn")

    def test_a_missing_staging_dir_fails(self, tmp_path):
        config = _config(tmp_path, staging_path=tmp_path / "nope")
        check = diagnostics.check_scratch(config)
        assert check["status"] == "fail"
        assert "staging" in check["detail"]


class TestRunChecks:
    def test_every_check_is_present(self, tmp_path):
        result = diagnostics.run_checks(_config(tmp_path))
        ids = {c["id"] for c in result["checks"]}
        assert ids == {"drives", "tools", "makemkv_key", "destination", "scratch", "database"}

    def test_a_check_that_explodes_does_not_hide_the_others(self, tmp_path, monkeypatch):
        def _boom():
            raise RuntimeError("sysfs went away")
        monkeypatch.setattr(diagnostics, "check_drives", _boom)

        result = diagnostics.run_checks(_config(tmp_path))
        assert len(result["checks"]) == 6, "a broken drive must not hide a full disk"
        drives = next(c for c in result["checks"] if c["id"] == "drives")
        assert drives["status"] == "warn"
        assert "sysfs went away" in drives["detail"]

    def test_the_ctid_is_substituted_into_fixes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ADR_CTID", "108")
        monkeypatch.setattr(diagnostics, "diagnose_passthrough", lambda: {
            "drives": [], "problems": ["broken"], "ok": False,
        })
        result = diagnostics.run_checks(_config(tmp_path))
        drives = next(c for c in result["checks"] if c["id"] == "drives")
        assert drives["fix"] == "adr-doctor --fix 108"
        assert "{ctid}" not in drives["fix"]

    def test_an_unknown_ctid_leaves_a_placeholder_not_a_broken_command(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.delenv("ADR_CTID", raising=False)
        monkeypatch.setattr(diagnostics, "diagnose_passthrough", lambda: {
            "drives": [], "problems": ["broken"], "ok": False,
        })
        result = diagnostics.run_checks(_config(tmp_path))
        drives = next(c for c in result["checks"] if c["id"] == "drives")
        assert "<CTID>" in drives["fix"]

    def test_failing_counts_only_failures(self, tmp_path, monkeypatch):
        monkeypatch.setattr(diagnostics, "check_drives", lambda: diagnostics._check(
            "drives", "Optical drives", "fail", "broken", "fix me"))
        monkeypatch.setattr(diagnostics, "check_tools", lambda: diagnostics._check(
            "tools", "Tools", "warn", "eh"))
        result = diagnostics.run_checks(_config(tmp_path))
        assert result["failing"] == 1
        assert result["ok"] is False


@pytest.mark.parametrize("status", ["ok", "warn", "fail"])
def test_check_shape_is_stable(status):
    """The UI indexes these keys directly."""
    check = diagnostics._check("x", "X", status, "detail")
    assert set(check) == {"id", "title", "status", "detail", "fix"}
