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
            "update_chain",
        }

    def test_a_check_that_explodes_does_not_hide_the_others(self, tmp_path, monkeypatch):
        def _boom():
            raise RuntimeError("sysfs went away")
        monkeypatch.setattr(diagnostics, "check_drives", _boom)

        result = diagnostics.run_checks(_config(tmp_path))
        assert len(result["checks"]) == 10, "a broken drive must not hide a full disk"
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
            "runtime": {"ok": True, "drivers": ["iHD_drv_video.so"],
                        "detail": "", "fix": ""},
        })
        check = diagnostics.check_hardware_encoding(_config(tmp_path))
        assert check["status"] == "ok"
        assert "renderD128" in check["detail"]

    def test_a_gpu_with_no_driver_is_not_reported_as_working(
        self, tmp_path, monkeypatch,
    ):
        """Green here would be the most expensive kind of lie: the render node
        genuinely is passed through, so the obvious check passes, and every
        encode still dies at initialisation for a reason nothing on this page
        would otherwise mention."""
        monkeypatch.setattr("adr.gpu.preset_wants_hardware", lambda f, n: "qsv_h264")
        monkeypatch.setattr("adr.gpu.describe", lambda: {
            "available": True, "nodes": ["/dev/dri/renderD128"],
            "detail": "present", "fix": "",
            "runtime": {
                "ok": False, "drivers": [],
                "detail": "the driver stack this GPU needs is not installed.",
                "fix": "Run on the Proxmox host: adr-doctor --fix {ctid}",
            },
        })
        check = diagnostics.check_hardware_encoding(_config(tmp_path))
        assert check["status"] == "fail"
        assert "driver stack" in check["detail"], "it must say which half is missing"
        assert "adr-doctor --fix" in check["fix"]

    def test_a_gpu_nobody_asked_for_is_not_a_problem(self, tmp_path, monkeypatch):
        """Noise about hardware someone never wanted trains people to ignore
        the page."""
        monkeypatch.setattr("adr.gpu.preset_wants_hardware", lambda f, n: "")
        monkeypatch.setattr("adr.gpu.describe", lambda: {
            "available": True, "nodes": ["/dev/dri/renderD128"],
            "detail": "present", "fix": "",
        })
        assert diagnostics.check_hardware_encoding(_config(tmp_path))["status"] == "ok"


class TestTheBundleCarriesTheQsvVerdict:
    """The bundle is what gets pasted, so the answer has to be in it.

    Without this the exchange is: paste the bundle, read "qsv runtime
    libmfxhw64.so" and "hb encoders none", and still not know whether the
    runtime was missing, refused, or never asked for.
    """

    def _config(self, tmp_path, **over):
        import types

        data = {
            "handbrake_path": str(tmp_path / "HandBrakeCLI"),
            "handbrake_preset": "Super HQ 1080p30 Surround (Svenska)",
            "handbrake_preset_file": "",
            "encoder_backend": "handbrake",
            "audio_language": "swe", "video_quality": 0, "max_height": 0,
            "ffmpeg_path": str(tmp_path / "ffmpeg"),
            "vaapi_device": "", "vaapi_codec": "h264", "libva_driver": "",
        }
        data.update(over)
        return types.SimpleNamespace(**data)

    def test_it_is_asked_when_a_hardware_preset_has_no_hardware_encoder(
        self, tmp_path, monkeypatch,
    ):
        from adr import bundle

        asked = []
        monkeypatch.setattr(
            "adr.gpu.qsv_dispatcher_log",
            lambda path, driver="iHD": asked.append(path) or {
                "ran": True, "driver": driver,
                "log": "libvpl: unloading libmfxhw64.so.1\n",
                "summary": "The dispatcher opened libmfxhw64.so and turned it down.",
            },
        )
        monkeypatch.setattr("adr.encodertest.build_hardware_encoders", lambda p: [])
        monkeypatch.setattr(
            "adr.gpu.preset_wants_hardware", lambda f, n: "qsv_h264",
        )
        text = bundle._hardware(self._config(tmp_path))
        assert asked, "the dispatcher was never asked"
        assert "qsv verdict" in text
        assert "turned it down" in text
        assert "oneVPL dispatcher log" in text

    def test_a_working_handbrake_needs_no_post_mortem(self, tmp_path, monkeypatch):
        from adr import bundle

        asked = []
        monkeypatch.setattr(
            "adr.gpu.qsv_dispatcher_log",
            lambda path, driver="iHD": asked.append(path) or {},
        )
        monkeypatch.setattr(
            "adr.encodertest.build_hardware_encoders", lambda p: ["qsv_h264"],
        )
        monkeypatch.setattr(
            "adr.gpu.preset_wants_hardware", lambda f, n: "qsv_h264",
        )
        bundle._hardware(self._config(tmp_path))
        assert asked == [], "a working setup was made to run a diagnostic"

    def test_a_software_preset_is_not_interrogated_about_quick_sync(
        self, tmp_path, monkeypatch,
    ):
        from adr import bundle

        asked = []
        monkeypatch.setattr(
            "adr.gpu.qsv_dispatcher_log",
            lambda path, driver="iHD": asked.append(path) or {},
        )
        monkeypatch.setattr("adr.encodertest.build_hardware_encoders", lambda p: [])
        monkeypatch.setattr("adr.gpu.preset_wants_hardware", lambda f, n: "")
        bundle._hardware(self._config(tmp_path))
        assert asked == []


class TestNothingThatAuthenticatesLeaves:
    """The bundle is written to be pasted in public, and one rode out with a
    live TMDb key in it.

    The settings section had been careful since the day it was written. The
    service log had not — because nothing put a secret in a log, until
    log_level DEBUG turned on urllib3, which writes every request URL in full:

        https://api.themoviedb.org:443 "GET /3/search/movie?api_key=<the key>"

    Redacting section by section is a rule someone has to remember at the
    moment they add a section. These pin down the rule applied where it cannot
    be forgotten.
    """

    def _config(self, **over):
        import types

        data = {
            "tmdb_api_key": "138d533b8fde764203da07e28c6aa8c6",
            "plex_token": "sX7pQm2vNbKd91La",
            "notify_token": "tk_abcdefghijklmnop",
            "notify_url": "https://ntfy.sh/my-private-topic-9f2a",
            "handbrake_preset": "Super HQ 1080p30 Surround (Svenska)",
            "completed_path": "/mnt/media",
            "log_level": "DEBUG",
        }
        data.update(over)
        return types.SimpleNamespace(as_dict=lambda: data, **data)

    def test_the_key_urllib3_logged_does_not_survive(self):
        from adr import bundle

        config = self._config()
        log = (
            'https://api.themoviedb.org:443 "GET /3/search/movie'
            '?api_key=138d533b8fde764203da07e28c6aa8c6&query=Dinosaur" 200 None'
        )
        out = bundle.scrub(log, config)
        assert "138d533b8fde764203da07e28c6aa8c6" not in out
        assert bundle.REDACTED in out

    def test_a_key_that_was_never_configured_is_still_caught(self):
        """Matched by the name beside it, because the value is exactly what is
        unknown — a second instance's key in a copied log, a provider this
        install does not use."""
        from adr import bundle

        out = bundle.scrub(
            "GET /3/movie/550?api_key=deadbeefcafebabe0123456789abcdef", self._config(),
        )
        assert "deadbeefcafebabe" not in out

    def test_every_configured_secret_is_hunted_for(self):
        from adr import bundle

        config = self._config()
        text = " ".join([
            "tmdb=138d533b8fde764203da07e28c6aa8c6",
            "plex=sX7pQm2vNbKd91La",
            "ntfy=tk_abcdefghijklmnop",
            "url=https://ntfy.sh/my-private-topic-9f2a",
        ])
        out = bundle.scrub(text, config)
        for secret in ("138d533b8fde764203da07e28c6aa8c6", "sX7pQm2vNbKd91La",
                       "tk_abcdefghijklmnop", "my-private-topic-9f2a"):
            assert secret not in out, secret

    def test_a_plex_token_header_is_caught(self):
        from adr import bundle

        out = bundle.scrub("X-Plex-Token: zzzzzzzzzzzzzzzzzzzz", self._config())
        assert "zzzzzzzzzzzzzzzzzzzz" not in out

    def test_an_authorization_header_is_caught(self):
        from adr import bundle

        out = bundle.scrub(
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload", self._config(),
        )
        assert "eyJhbGciOiJIUzI1NiJ9" not in out

    def test_safe_settings_are_left_alone(self):
        """A bundle redacted into uselessness answers nothing. The preset name
        and the destination are the two things every diagnosis needs."""
        from adr import bundle

        out = bundle.scrub(
            "preset 'Super HQ 1080p30 Surround (Svenska)' to /mnt/media", self._config(),
        )
        assert "Super HQ 1080p30 Surround (Svenska)" in out
        assert "/mnt/media" in out

    def test_a_short_value_is_not_hunted_across_the_document(self):
        """Blanking every short string that happens to appear would redact the
        bundle into noise."""
        from adr import bundle

        config = self._config(plex_section="1")
        assert "1080p30" in bundle.scrub("preset 1080p30", config)

    def test_the_whole_bundle_goes_through_it(self):
        """Not the sections individually — the rule has to hold for a section
        that does not exist yet."""
        import inspect

        from adr import bundle

        source = inspect.getsource(bundle.build)
        assert "return scrub(" in source, "build() stopped scrubbing its output"

    def test_a_config_that_cannot_be_read_still_scrubs_by_pattern(self):
        import types

        from adr import bundle

        def _boom():
            raise RuntimeError("no config")

        broken = types.SimpleNamespace(as_dict=_boom)
        out = bundle.scrub("api_key=deadbeefcafebabe", broken)
        assert "deadbeefcafebabe" not in out


class TestTheLogDoesNotGetTheSecretInTheFirstPlace:
    def test_urllib3_is_held_at_info_however_verbose_the_app_is(self):
        import logging

        from adr import applog

        logging.getLogger("urllib3.connectionpool").setLevel(logging.DEBUG)
        applog.quieten_request_logging()
        assert logging.getLogger("urllib3.connectionpool").level == logging.INFO
        assert logging.getLogger("urllib3").level == logging.INFO


class TestTheUpdateChainReportsItself:
    """The button writes a flag file, adr-update.path has to notice it, and
    the service it starts has to have something to execute. A break in any
    link is silent — the button simply does nothing — and 1.31 broke the third
    one for every installation updating from an older version.
    """

    def _run(self, monkeypatch, *, unit=True, watcher=True, executable=True):
        from pathlib import Path as _Path

        from adr import diagnostics

        real_exists = _Path.exists

        def fake_exists(self):
            if str(self) == "/etc/systemd/system/adr-update.service":
                return unit
            if str(self) == "/usr/local/lib/adr/update.sh":
                return executable
            return real_exists(self)

        monkeypatch.setattr(_Path, "exists", fake_exists)
        monkeypatch.setattr("adr.updater._unit_active", lambda u: watcher)
        monkeypatch.setattr(diagnostics.os, "access", lambda p, m: executable)
        return diagnostics.check_update_chain()

    def test_a_healthy_chain_says_the_button_works(self, monkeypatch):
        result = self._run(monkeypatch)
        assert result["status"] == "ok"
        assert "the button in the web UI works" in result["detail"]

    def test_a_missing_executable_is_named(self, monkeypatch):
        """The exact break 1.31 shipped: the unit installed, its ExecStart
        absent, and nothing on screen saying so."""
        result = self._run(monkeypatch, executable=False)
        assert result["status"] == "fail"
        assert "/usr/local/lib/adr/update.sh is missing" in result["detail"]
        assert "update.sh" in result["fix"]

    def test_a_stopped_watcher_is_named(self, monkeypatch):
        result = self._run(monkeypatch, watcher=False)
        assert result["status"] == "fail"
        assert "would go unnoticed" in result["detail"]

    def test_both_breaks_are_reported_together(self, monkeypatch):
        """Fixing one and finding the other still broken is two trips to the
        host."""
        result = self._run(monkeypatch, watcher=False, executable=False)
        assert "not running" in result["detail"]
        assert "is missing" in result["detail"]

    def test_an_install_predating_updates_is_a_warning_not_a_failure(
        self, monkeypatch,
    ):
        """Nothing is broken there — the feature simply arrived later."""
        result = self._run(monkeypatch, unit=False)
        assert result["status"] == "warn"
        assert "predates" in result["detail"]
