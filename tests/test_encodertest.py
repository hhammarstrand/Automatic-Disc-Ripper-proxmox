"""Ask HandBrake whether it can encode, without needing a disc.

A preset HandBrake cannot satisfy fails identically on every title of every
disc: exit 3, an initialisation error, forty minutes after the disc went in
and once per title. Ten titles, ten identical failures, and the cause is a
line of stderr nobody sees. It can be answered in seconds instead.
"""

import stat
import textwrap
import types

import pytest

from adr import encodertest


def _script(path, body):
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _config(tmp_path, **overrides):
    data = {
        "handbrake_path": overrides.get("handbrake_path", "/bin/true"),
        "handbrake_preset": overrides.get("handbrake_preset", "Fast 1080p30"),
        "handbrake_preset_file": overrides.get("handbrake_preset_file", ""),
        "handbrake_extra_args": overrides.get("handbrake_extra_args", ""),
        "ffmpeg_path": overrides.get("ffmpeg_path", "/bin/true"),
    }
    return types.SimpleNamespace(**data)


class TestWhatItReportsFirst:
    def test_a_missing_handbrake_stops_there(self, tmp_path):
        result = encodertest.test_encoder(
            _config(tmp_path, handbrake_path=str(tmp_path / "nope")))
        assert result["ok"] is False
        assert len(result["steps"]) == 1
        assert "not installed" in result["steps"][0]["detail"]

    def test_the_fix_carries_a_runnable_command(self, tmp_path):
        result = encodertest.with_ctid(
            encodertest.test_encoder(
                _config(tmp_path, handbrake_path=str(tmp_path / "nope"))),
            "108")
        assert "pct exec 108" in result["steps"][0]["fix"]
        assert "{ctid}" not in result["steps"][0]["fix"]


class TestThePresetProbe:
    def test_a_preset_handbrake_knows_passes(self, tmp_path):
        hb = _script(tmp_path / "HandBrakeCLI", 'echo "Fast 1080p30"\n')
        step = encodertest._preset_step(hb, "", "Fast 1080p30")
        assert step["status"] == "ok"

    def test_a_preset_it_does_not_know_is_flagged(self, tmp_path):
        hb = _script(tmp_path / "HandBrakeCLI", 'echo "Fast 1080p30"\n')
        step = encodertest._preset_step(hb, "", "Super HQ Surround")
        assert step["status"] == "warn"
        assert "Super HQ Surround" in step["detail"]

    def test_the_imported_file_is_named(self, tmp_path):
        hb = _script(tmp_path / "HandBrakeCLI", "echo nothing\n")
        preset = tmp_path / "p.json"
        preset.write_text("{}")
        step = encodertest._preset_step(hb, str(preset), "Mine")
        assert str(preset) in step["detail"]

    def test_a_handbrake_that_will_not_run_is_a_failure(self, tmp_path):
        step = encodertest._preset_step(str(tmp_path / "gone"), "", "Fast 1080p30")
        assert step["status"] == "fail"


class TestTheEncodeProbe:
    def test_a_working_encoder_passes(self, tmp_path):
        """The stub writes whatever output file it is told to."""
        ffmpeg = _script(tmp_path / "ffmpeg", 'for last; do :; done; printf x > "$last"\n')
        hb = _script(tmp_path / "HandBrakeCLI", '''
            for a in "$@"; do
              if [ "$prev" = "-o" ]; then printf video > "$a"; fi
              prev="$a"
            done
        ''')
        config = _config(tmp_path, handbrake_path=hb, ffmpeg_path=ffmpeg)
        step = encodertest._encode_step(hb, "", config)
        assert step["status"] == "ok", step["detail"]

    def test_a_failing_encoder_reports_what_it_said(self, tmp_path):
        ffmpeg = _script(tmp_path / "ffmpeg", 'for last; do :; done; printf x > "$last"\n')
        hb = _script(tmp_path / "HandBrakeCLI",
                     'echo "ERROR: Unknown video encoder nvenc_h265" >&2\nexit 3\n')
        config = _config(tmp_path, handbrake_path=hb, ffmpeg_path=ffmpeg)
        step = encodertest._encode_step(hb, "", config)
        assert step["status"] == "fail"
        assert "nvenc_h265" in step["detail"]
        assert "exit 3" in step["detail"]

    def test_without_ffmpeg_it_says_so_rather_than_blaming_handbrake(self, tmp_path):
        config = _config(tmp_path, ffmpeg_path=str(tmp_path / "no-ffmpeg"))
        step = encodertest._encode_step("/bin/true", "", config)
        assert step["status"] == "warn"
        assert "ffmpeg is not installed" in step["detail"]

    def test_a_sample_that_cannot_be_made_is_not_handbrakes_fault(self, tmp_path):
        ffmpeg = _script(tmp_path / "ffmpeg", 'echo "no such filter" >&2\nexit 1\n')
        config = _config(tmp_path, ffmpeg_path=ffmpeg)
        step = encodertest._encode_step("/bin/true", "", config)
        assert step["status"] == "warn"
        assert "sample" in step["detail"]


class TestTurningHandBrakeIntoAdvice:
    """Exit code 3 is every initialisation failure there is, so the advice has
    to come from what it said, not from the number."""

    @pytest.mark.parametrize("said,expected", [
        ("ERROR: Invalid preset", "preset name does not exist"),
        ("Unknown video encoder nvenc_h264", "hardware encoder"),
        ("encoder x265_10bit not supported", "compiled without"),
        ("Failed to set up audio track", "audio"),
    ])
    def test_it_names_something_to_do(self, said, expected):
        assert expected in encodertest._explain(said)

    def test_something_unrecognised_gets_no_invented_advice(self):
        assert encodertest._explain("something nobody has seen before") == ""

    def test_hardware_is_recognised_before_the_generic_encoder_rule(self):
        """A GPU that is not in the container needs different advice from a
        codec the build lacks."""
        advice = encodertest._explain("Unknown video encoder nvenc_h265")
        assert "hardware" in advice
        assert "compiled without" not in advice


class TestChoosingWhatToShow:
    def test_error_lines_win_over_chatter(self):
        text = "\n".join([
            "Scanning title 1",
            "ERROR: Invalid preset",
            "HandBrake has exited.",
        ])
        assert "Invalid preset" in encodertest._meaningful(text)

    def test_without_errors_the_last_lines_are_shown(self):
        assert "second" in encodertest._meaningful("first\nsecond")

    def test_noise_is_dropped(self):
        text = "hb_display_init: attempting VA driver\nERROR: real problem"
        out = encodertest._meaningful(text)
        assert "hb_display" not in out
        assert "real problem" in out

    def test_it_does_not_dump_the_whole_log(self):
        text = "\n".join(f"ERROR: line {i}" for i in range(50))
        assert len(encodertest._meaningful(text).splitlines()) <= 6


#: What `HandBrakeCLI --preset-list` actually prints: categories at the left
#: margin ending in a slash, names one level in, descriptions one level
#: further and wrapped across as many lines as they need.
PRESET_LIST = """\

General/

    Very Fast 1080p30
        Small H.264 video (up to 1080p30) and AAC stereo audio, in an MP4
        container.

    Fast 1080p30
        Small H.264 video (up to 1080p30), AAC stereo audio, and Dolby
        Digital (AC-3) surround audio, in an MP4 container.

Hardware/

    H.264 QSV 1080p30
        H.264 video (up to 1080p30) using Intel Quick Sync Video hardware
        acceleration, and AAC stereo audio, in an MP4 container.

Matroska/

    H.265 MKV 2160p60
        High quality H.265 video (up to 2160p60) and surround audio, in an
        MKV container.
"""


class TestReadingThePresetList:
    """The list HandBrake prints is indentation, not lines.

    Guessing at a range of indents catches the wrapped description lines too,
    which is how "and Dolby Digital (AC-3) surround audio, in an MP4" ended up
    in a dropdown as something to encode with.
    """

    @pytest.fixture
    def handbrake(self, tmp_path):
        return _script(
            tmp_path / "hb",
            f"""
            cat <<'PRESETS'
{PRESET_LIST}PRESETS
            """,
        )

    def test_it_finds_the_preset_names(self, handbrake):
        assert encodertest._builtin_presets(handbrake) == [
            "Very Fast 1080p30",
            "Fast 1080p30",
            "H.264 QSV 1080p30",
            "H.265 MKV 2160p60",
        ]

    def test_no_description_text_survives(self, handbrake):
        for name in encodertest._builtin_presets(handbrake):
            assert "container" not in name
            assert not name.endswith(",")

    def test_categories_are_not_presets(self, handbrake):
        names = encodertest._builtin_presets(handbrake)
        assert "General/" not in names
        assert "General" not in names

    def test_a_different_indent_width_still_works(self, tmp_path):
        """The name level is measured, not assumed, so HandBrake changing
        its spacing does not empty the dropdown."""
        exe = _script(tmp_path / "hb2", """
            printf 'General/\\n'
            printf '  Fast 1080p30\\n'
            printf '    A description that wraps\\n'
            printf '    onto a second line.\\n'
        """)
        assert encodertest._builtin_presets(exe) == ["Fast 1080p30"]

    def test_a_handbrake_that_will_not_run_returns_nothing(self, tmp_path):
        assert encodertest._builtin_presets(str(tmp_path / "absent")) == []

    def test_hardware_presets_are_kept_out_of_the_software_list(self, tmp_path, handbrake):
        config = _config(tmp_path, handbrake_path=handbrake,
                         handbrake_preset="Fast 1080p30 QSV")
        offered = encodertest.software_alternatives(config)
        assert "H.264 QSV 1080p30" not in offered

    def test_the_closest_software_preset_is_offered_first(self, tmp_path, handbrake):
        config = _config(tmp_path, handbrake_path=handbrake,
                         handbrake_preset="Fast 1080p30 (Svenska)")
        assert encodertest.software_alternatives(config)[0] == "Fast 1080p30"

    def test_every_software_preset_is_still_offered(self, tmp_path, handbrake):
        """Resemblance orders the list; it must not shorten it."""
        config = _config(tmp_path, handbrake_path=handbrake,
                         handbrake_preset="Nothing Like Any Of Them")
        assert len(encodertest.software_alternatives(config)) == 3


class TestAdviceWhenThePresetWantsAGPU:
    """Three different situations produce the same HandBrake error and have
    completely different fixes. Getting this wrong costs a working setup."""

    def _gpu(self, monkeypatch, **state):
        from adr import gpu

        base = {"available": False, "nodes": [], "detail": "", "fix": "",
                "runtime": {"ok": False, "drivers": [], "detail": "", "fix": ""}}
        base.update(state)
        monkeypatch.setattr(gpu, "describe", lambda: base)

    def test_no_gpu_at_all_offers_both_ways_out(self, monkeypatch):
        self._gpu(monkeypatch, detail="No GPU here.",
                  fix="Run on the Proxmox host: adr-doctor --fix {ctid}")
        advice = encodertest._hardware_advice()
        assert "adr-doctor --fix" in advice
        assert "software preset" in advice

    def test_a_passed_through_gpu_with_no_driver_does_not_say_give_up(
        self, monkeypatch,
    ):
        """The case that looks solved. The node is there, so every check about
        passthrough passes, and the encode still dies — because the driver on
        top of it was never installed. Sending someone who has just finished
        passing a GPU through back to software is the worst answer available.
        """
        self._gpu(
            monkeypatch, available=True, nodes=["/dev/dri/renderD128"],
            runtime={"ok": False, "drivers": [], "detail": "",
                     "fix": "Run on the Proxmox host: adr-doctor --fix {ctid}"},
        )
        advice = encodertest._hardware_advice()
        assert "driver" in advice
        assert "adr-doctor --fix" in advice
        assert "missing from this HandBrake build" not in advice

    def test_a_gpu_with_its_driver_blames_the_build(self, monkeypatch):
        """Node present, driver installed, and it still fails — now the build
        really is the remaining explanation."""
        self._gpu(
            monkeypatch, available=True, nodes=["/dev/dri/renderD128"],
            runtime={"ok": True, "drivers": ["iHD_drv_video.so"],
                     "detail": "", "fix": ""},
        )
        advice = encodertest._hardware_advice()
        assert "HandBrake build" in advice
        assert "x264" in advice
