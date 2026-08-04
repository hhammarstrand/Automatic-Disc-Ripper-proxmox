"""Settings that mean the same thing whichever encoder runs.

Two encoders do the transcoding, and until this module they were configured in
two unrelated ways — a HandBrake preset on one side, a handful of vaapi_*
settings on the other. So "I want Swedish audio" was a preset property in one
and a setting in the other, and switching encoders silently changed what came
out. These tests pin down that the shared settings reach both, and that a
default reaches neither.
"""

import types

import pytest

from adr import encodingsettings


def _config(**overrides):
    data = {"audio_language": "", "video_quality": 0, "max_height": 0}
    data.update(overrides)
    return types.SimpleNamespace(**data)


class TestLeavingThePresetAlone:
    """Nothing here overrides a preset unless it is asked to. Someone with a
    carefully tuned preset must be able to install an update without their
    settings quietly changing underneath them."""

    def test_defaults_produce_no_arguments_at_all(self):
        assert encodingsettings.handbrake_overrides(_config()) == []

    @pytest.mark.parametrize("field", ["audio_language", "video_quality", "max_height"])
    def test_each_setting_is_independent(self, field):
        """Setting one must not drag the others in."""
        value = "swe" if field == "audio_language" else 20
        args = encodingsettings.handbrake_overrides(_config(**{field: value}))
        assert args, "the one that was set does appear"
        assert len(args) <= 3

    def test_a_nonsense_quality_is_ignored_rather_than_passed_on(self):
        assert encodingsettings.handbrake_overrides(_config(video_quality="lots")) == []

    def test_a_nonsense_height_is_ignored(self):
        assert encodingsettings.handbrake_overrides(_config(max_height="big")) == []


class TestWhatHandBrakeIsTold:
    def test_only_the_language_list_is_set(self):
        """How many matching tracks to take is the preset's
        AudioTrackSelectionBehavior. Forcing --all-audio here would override a
        deliberate choice with one nobody made: a preset that says "first"
        wants one track, and handing it five is a different setting, not a
        more generous reading of the same one."""
        args = encodingsettings.handbrake_overrides(_config(audio_language="swe"))
        assert args == ["--audio-lang-list", "swe"]

    def test_a_two_letter_code_is_translated(self):
        """A disc tags "swe" and a person types "sv"."""
        args = encodingsettings.handbrake_overrides(_config(audio_language="sv"))
        assert "swe" in args
        assert "sv" not in args

    def test_the_height_is_a_cap(self):
        args = encodingsettings.handbrake_overrides(_config(max_height=1080))
        assert args == ["--maxHeight", "1080"]

    def test_quality_is_the_quantiser_flag(self):
        assert encodingsettings.handbrake_overrides(_config(video_quality=20)) == ["-q", "20"]

    def test_quality_is_clamped_to_something_sane(self):
        assert encodingsettings.handbrake_overrides(_config(video_quality=99))[1] == "35"
        assert encodingsettings.handbrake_overrides(_config(video_quality=2))[1] == "15"


class TestSayingWhatWillHappen:
    def test_it_describes_the_defaults_honestly(self):
        said = encodingsettings.describe(_config())
        assert "disc's own audio order" in said
        assert "source resolution" in said
        assert "preset's own quality" in said

    def test_it_describes_what_was_asked_for(self):
        said = encodingsettings.describe(
            _config(audio_language="swe", video_quality=20, max_height=1080))
        assert "swe" in said and "1080p" in said and "20" in said

    def test_the_backend_line_names_both_the_tool_and_the_result(self):
        """Naming only the program is how someone ends up believing a
        HandBrake preset governs an ffmpeg encode."""
        from adr.encoderfactory import describe_backend

        config = _config(audio_language="swe", encoder_backend="vaapi",
                         vaapi_codec="h264")
        said = describe_backend(config)
        assert "ffmpeg" in said
        assert "swe" in said


class TestBothEncodersActuallyReadThem:
    def test_the_gpu_encoder_takes_the_shared_quality(self, tmp_path):
        from pathlib import Path

        from adr import vaapi

        config = types.SimpleNamespace(
            vaapi_device="/dev/dri/renderD128", vaapi_codec="h264",
            video_quality=18, max_height=1080,
        )
        cmd = vaapi.build_command("ffmpeg", Path("i.mkv"), tmp_path / "o.mp4", config)
        assert cmd[cmd.index("-qp") + 1] == "18"
        assert "min(1080,ih)" in cmd[cmd.index("-vf") + 1].replace(" ", "")

    def test_an_old_config_keeps_the_number_it_was_given(self, tmp_path):
        """A settings migration that silently resets someone's quality is
        worse than one that never happened."""
        from adr.config import Config

        config = Config.__new__(Config)
        config._data = {"vaapi_quality": 19, "vaapi_max_height": 720}
        assert config.video_quality == 19
        assert config.max_height == 720

    def test_the_new_key_wins_when_both_are_present(self):
        from adr.config import Config

        config = Config.__new__(Config)
        config._data = {"video_quality": 21, "vaapi_quality": 19}
        assert config.video_quality == 21

    def test_the_encoder_test_runs_the_same_overrides(self, tmp_path):
        """A test that skipped them would pass on a flag HandBrake rejects,
        and the rejection would surface forty minutes into a rip instead."""
        import stat
        import textwrap

        from adr import encodertest

        # A HandBrake that records what it was asked to do.
        seen = tmp_path / "argv"
        hb = tmp_path / "hb"
        hb.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            echo "$@" >> {seen}
            for a in "$@"; do
              if [ "$prev" = "-o" ]; then printf video > "$a"; fi
              prev="$a"
            done
            exit 0
        """))
        hb.chmod(hb.stat().st_mode | stat.S_IXUSR)
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_text('#!/bin/sh\nfor last; do :; done\nprintf x > "$last"\n')
        ffmpeg.chmod(ffmpeg.stat().st_mode | stat.S_IXUSR)

        # An explicit software preset, so the run goes straight to the encode
        # rather than through the hardware probe.
        preset = tmp_path / "preset.json"
        preset.write_text(
            '{"PresetList": [{"PresetName": "Mine", "VideoEncoder": "x264"}]}')

        config = types.SimpleNamespace(
            handbrake_path=str(hb), handbrake_preset="Mine",
            handbrake_preset_file=str(preset), handbrake_extra_args="",
            ffmpeg_path=str(ffmpeg), encoder_backend="handbrake",
            audio_language="swe", video_quality=20, max_height=1080,
        )
        encodertest.test_encoder(config)
        recorded = seen.read_text()
        assert "--audio-lang-list swe" in recorded
        assert "--maxHeight 1080" in recorded
        assert "-q 20" in recorded


class TestTheStatusLineDescribesTheRightProgram:
    """A line that describes the encoder which is *not* going to run is worse
    than no line: it is confidently wrong about the one thing it exists to
    report."""

    def test_handbrake_defers_to_its_preset(self):
        said = encodingsettings.describe(
            _config(encoder_backend="handbrake"))
        assert "preset's own quality" in said

    def test_the_gpu_path_has_no_preset_to_defer_to(self):
        said = encodingsettings.describe(_config(encoder_backend="vaapi"))
        assert "preset" not in said
        assert "encoder's default quality" in said

    def test_a_set_quality_is_named_whichever_encoder_runs(self):
        for backend in ("handbrake", "vaapi"):
            said = encodingsettings.describe(
                _config(encoder_backend=backend, video_quality=20))
            assert "quality 20" in said
