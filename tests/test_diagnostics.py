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
        "handbrake_preset": "Fast 1080p30",
        "handbrake_preset_file": "",
        "auto_move_to_plex": True,
        "require_completed_mount": False,
        "handbrake_path": "/usr/bin/HandBrakeCLI",
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


class TestAudioTools:
    def test_a_machine_that_never_sees_a_cd_is_not_broken(self, tmp_path):
        config = _config(tmp_path, audio_cd_enabled=False)
        check = diagnostics.check_audio_tools(config)
        assert check["status"] == "ok"
        assert "turned off" in check["detail"]

    def test_installed_tools_pass(self, tmp_path):
        config = _config(tmp_path, audio_cd_enabled=True,
                         cdparanoia_path="/bin/true", ffmpeg_path="/bin/true")
        assert diagnostics.check_audio_tools(config)["status"] == "ok"

    def test_missing_tools_warn_rather_than_fail(self, tmp_path):
        """Video discs still work without them, so this must not read as a
        broken installation."""
        config = _config(tmp_path, audio_cd_enabled=True,
                         cdparanoia_path="/nowhere/cdparanoia", ffmpeg_path="/nowhere/ffmpeg")
        check = diagnostics.check_audio_tools(config)
        assert check["status"] == "warn"
        assert "cdparanoia" in check["detail"]
        assert "video discs are unaffected" in check["detail"].lower()
        assert "apt-get install" in check["fix"]


class TestRunChecks:
    def test_every_check_is_present(self, tmp_path):
        result = diagnostics.run_checks(_config(tmp_path))
        ids = {c["id"] for c in result["checks"]}
        assert ids == {
            "drives", "tools", "preset", "makemkv_key", "audio_tools",
            "hardware_encoding", "destination", "scratch", "database",
        }

    def test_a_check_that_explodes_does_not_hide_the_others(self, tmp_path, monkeypatch):
        def _boom():
            raise RuntimeError("sysfs went away")
        monkeypatch.setattr(diagnostics, "check_drives", _boom)

        result = diagnostics.run_checks(_config(tmp_path))
        assert len(result["checks"]) == 9, "a broken drive must not hide a full disk"
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

    def test_a_warning_is_not_counted_as_a_failure(self, tmp_path, monkeypatch):
        """Asserts on the two checks it controls — the rest depend on the
        machine the suite happens to run on, and pinning a total count makes
        this pass as root and fail as anyone else."""
        monkeypatch.setattr(diagnostics, "check_drives", lambda: diagnostics._check(
            "drives", "Optical drives", "fail", "broken", "fix me"))
        monkeypatch.setattr(diagnostics, "check_tools", lambda: diagnostics._check(
            "tools", "Tools", "warn", "eh"))
        result = diagnostics.run_checks(_config(tmp_path))

        by_id = {c["id"]: c for c in result["checks"]}
        assert by_id["drives"]["status"] == "fail"
        assert by_id["tools"]["status"] == "warn"
        assert result["failing"] >= 1
        assert result["failing"] == sum(1 for c in result["checks"] if c["status"] == "fail")
        assert result["ok"] is False


@pytest.mark.parametrize("status", ["ok", "warn", "fail"])
def test_check_shape_is_stable(status):
    """The UI indexes these keys directly."""
    check = diagnostics._check("x", "X", status, "detail")
    assert set(check) == {"id", "title", "status", "detail", "fix"}


class TestPreset:
    """A preset name missing from its file makes every encode fail identically,
    with HandBrake's output as the only clue."""

    def _config_with(self, tmp_path, name, file=""):
        return _config(tmp_path, handbrake_preset=name, handbrake_preset_file=str(file))

    def test_a_builtin_preset_needs_no_file(self, tmp_path):
        check = diagnostics.check_preset(self._config_with(tmp_path, "Fast 1080p30"))
        assert check["status"] == "ok"
        assert "built-in" in check["detail"]

    def test_a_missing_file_fails_with_the_path(self, tmp_path):
        check = diagnostics.check_preset(
            self._config_with(tmp_path, "Mine", tmp_path / "gone.json"))
        assert check["status"] == "fail"
        assert "gone.json" in check["detail"]

    def test_invalid_json_says_so(self, tmp_path):
        preset = tmp_path / "broken.json"
        preset.write_text("{not json")
        check = diagnostics.check_preset(self._config_with(tmp_path, "Mine", preset))
        assert check["status"] == "fail"
        assert "not valid JSON" in check["detail"]

    def test_a_name_that_is_not_in_the_file_lists_what_is(self, tmp_path):
        preset = tmp_path / "p.json"
        preset.write_text('{"PresetList": [{"PresetName": "Actual"}]}')
        check = diagnostics.check_preset(self._config_with(tmp_path, "Typo", preset))
        assert check["status"] == "fail"
        assert "Actual" in check["detail"], "say which names are available"

    def test_a_matching_name_passes(self, tmp_path):
        preset = tmp_path / "p.json"
        preset.write_text('{"PresetList": [{"PresetName": "Mine"}]}')
        check = diagnostics.check_preset(self._config_with(tmp_path, "Mine", preset))
        assert check["status"] == "ok"

    def test_the_flat_single_preset_format_is_understood(self, tmp_path):
        preset = tmp_path / "p.json"
        preset.write_text('{"PresetName": "Solo"}')
        check = diagnostics.check_preset(self._config_with(tmp_path, "Solo", preset))
        assert check["status"] == "ok"


class TestHardwareEncoding:
    """A preset that asks for a GPU the container does not have fails every
    title of every disc, identically, at initialisation."""

    def test_software_presets_need_no_gpu(self, tmp_path, monkeypatch):
        monkeypatch.setattr("adr.gpu.preset_wants_hardware", lambda f, n: "")
        monkeypatch.setattr("adr.gpu.describe", lambda: {
            "available": False, "nodes": [], "detail": "no /dev/dri", "fix": "",
        })
        check = diagnostics.check_hardware_encoding(
            _config(tmp_path, handbrake_preset="Fast 1080p30"))
        assert check["status"] == "ok"
        assert "software" in check["detail"]

    def test_a_hardware_preset_without_a_gpu_is_a_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("adr.gpu.preset_wants_hardware", lambda f, n: "qsv_h264")
        monkeypatch.setattr("adr.gpu.describe", lambda: {
            "available": False, "nodes": [],
            "detail": "/dev/dri does not exist in this container.",
            "fix": "Run on the Proxmox host: adr-doctor --fix {ctid}",
        })
        check = diagnostics.check_hardware_encoding(
            _config(tmp_path, handbrake_preset="Super HQ"))
        assert check["status"] == "fail"
        assert "qsv_h264" in check["detail"]
        assert "adr-doctor --fix" in check["fix"]

    def test_a_hardware_preset_with_a_gpu_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("adr.gpu.preset_wants_hardware", lambda f, n: "qsv_h264")
        monkeypatch.setattr("adr.gpu.describe", lambda: {
            "available": True, "nodes": ["/dev/dri/renderD128"],
            "detail": "present", "fix": "",
        })
        check = diagnostics.check_hardware_encoding(_config(tmp_path))
        assert check["status"] == "ok"
        assert "renderD128" in check["detail"]

    def test_a_gpu_nobody_asked_for_is_not_a_problem(self, tmp_path, monkeypatch):
        """Noise about hardware someone never wanted trains people to ignore
        the page."""
        monkeypatch.setattr("adr.gpu.preset_wants_hardware", lambda f, n: "")
        monkeypatch.setattr("adr.gpu.describe", lambda: {
            "available": True, "nodes": ["/dev/dri/renderD128"],
            "detail": "present", "fix": "",
        })
        assert diagnostics.check_hardware_encoding(_config(tmp_path))["status"] == "ok"
