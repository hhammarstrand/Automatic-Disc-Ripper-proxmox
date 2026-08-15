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
        import yaml

        from adr.config import Config

        path = tmp_path / "adr.yaml"
        path.write_text(yaml.safe_dump({
            "vaapi_quality": 19, "vaapi_max_height": 720,
            "completed_path": str(tmp_path), "raw_path": str(tmp_path),
            "staging_path": str(tmp_path),
        }))
        config = Config(str(path))
        assert config.video_quality == 19
        assert config.max_height == 720

    def test_the_old_name_is_gone_from_the_file_afterwards(self, tmp_path):
        """Leaving it there is what created two sources for one number: the
        settings page showing 0 while the encoder used 19, and nothing
        anywhere saying which was in charge."""
        import yaml

        from adr.config import Config

        path = tmp_path / "adr.yaml"
        path.write_text(yaml.safe_dump({
            "vaapi_quality": 19, "completed_path": str(tmp_path),
            "raw_path": str(tmp_path), "staging_path": str(tmp_path),
        }))
        Config(str(path))
        saved = yaml.safe_load(path.read_text())
        assert "vaapi_quality" not in saved
        assert saved["video_quality"] == 19

    def test_a_value_set_since_is_not_overwritten_by_the_old_one(self, tmp_path):
        """Someone who has since used the settings page means that value; a
        migration undoing it with a historical one is worse than none."""
        import yaml

        from adr.config import Config

        path = tmp_path / "adr.yaml"
        path.write_text(yaml.safe_dump({
            "video_quality": 21, "vaapi_quality": 19,
            "completed_path": str(tmp_path), "raw_path": str(tmp_path),
            "staging_path": str(tmp_path),
        }))
        config = Config(str(path))
        assert config.video_quality == 21
        assert "vaapi_quality" not in yaml.safe_load(path.read_text())

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


class TestThePresetIsTheTemplate:
    """The report: "Och den blev på engelska."

    The spoken language was set in the HandBrake preset, HandBrake could not
    reach the GPU, so the encoder was switched to ffmpeg — and ffmpeg read only
    the (empty) setting. The preset said Swedish and nothing was listening.
    """

    def _preset(self, tmp_path, name, languages, filename="P.json"):
        import json

        path = tmp_path / filename
        path.write_text(json.dumps({"PresetList": [{
            "PresetName": name, "AudioLanguageList": languages,
        }]}))
        return types.SimpleNamespace(
            audio_language="", video_quality=0, max_height=0,
            handbrake_preset=name, handbrake_preset_file=str(path),
        )

    def test_the_preset_s_language_is_used_when_the_setting_is_blank(self, tmp_path):
        config = self._preset(tmp_path, "Svenska", ["swe"])
        assert encodingsettings.preset_language(config) == "swe"
        assert encodingsettings.language(config) == "swe"

    def test_it_reaches_handbrake_s_arguments_too(self, tmp_path):
        """Redundant for HandBrake, which reads its own preset — but the two
        encoders now answer the same question the same way, which is the
        whole point of this module."""
        config = self._preset(tmp_path, "Svenska", ["swe"])
        assert "--audio-lang-list" in encodingsettings.handbrake_overrides(config)

    def test_the_setting_still_wins(self, tmp_path):
        """Someone typed it."""
        config = self._preset(tmp_path, "Svenska", ["swe"])
        config.audio_language = "nor"
        assert encodingsettings.language(config) == "nor"

    def test_a_two_letter_code_in_a_preset_is_translated(self, tmp_path):
        config = self._preset(tmp_path, "Svenska", ["sv"])
        assert encodingsettings.preset_language(config) == "swe"

    def test_any_and_und_are_not_languages(self, tmp_path):
        for placeholder in (["und"], ["any"], [""], []):
            config = self._preset(tmp_path, "P", placeholder)
            assert encodingsettings.preset_language(config) == ""

    def test_the_first_named_language_leads(self, tmp_path):
        config = self._preset(tmp_path, "P", ["swe", "eng"])
        assert encodingsettings.preset_language(config) == "swe"

    def test_a_preset_name_that_is_not_in_the_file_is_not_read(self, tmp_path):
        """HandBrake resolves a built-in name from its own list even with a
        file imported alongside. Reading a language out of that file would
        apply a setting from a preset that never runs."""
        config = self._preset(tmp_path, "Svenska", ["swe"])
        config.handbrake_preset = "Fast 1080p30"
        assert encodingsettings.preset_language(config) == ""

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        config = self._preset(tmp_path, "Svenska", ["swe"])
        config.handbrake_preset_file = str(tmp_path / "gone.json")
        assert encodingsettings.preset_language(config) == ""

    def test_broken_json_is_not_an_error(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        config = types.SimpleNamespace(
            audio_language="", video_quality=0, max_height=0,
            handbrake_preset="Svenska", handbrake_preset_file=str(path),
        )
        assert encodingsettings.preset_language(config) == ""

    def test_the_shipped_preset_asks_for_swedish(self):
        """The preset this repository installs. If this ever stops being true
        the fallback above is silently doing nothing."""
        import json
        from pathlib import Path

        files = sorted(Path("presets").glob("*.json"))
        assert files, "the shipped preset is gone"
        data = json.loads(files[0].read_text())
        entry = encodingsettings._find_preset(data, data["PresetList"][0]["PresetName"])
        assert entry["AudioLanguageList"] == ["swe"]


class TestAFilmIsNeverEncodedSilent:
    """The bug this class exists for: *The Black Cauldron*, *Jumanji* and
    *Charlotte's Web* all encoded with no audio at all and reported Done.

    HandBrake has no fallback. ``hb_preset_job_add_audio`` reaches for the
    wildcard only when the language list is *empty* — never when a non-empty
    list matched nothing — so ``--audio-lang-list swe`` on an American
    pressing selects no audio, writes the film mute, and exits 0. Old discs
    carry English and nothing else, which is exactly that case.
    """

    ENGLISH_ONLY = [{"codec": "ac3", "language": "eng"}]
    HAS_SWEDISH = [
        {"codec": "ac3", "language": "eng"},
        {"codec": "ac3", "language": "swe"},
    ]
    UNTAGGED = [{"codec": "ac3", "language": ""}]

    def _config(self, **values):
        base = {
            "audio_language": "swe", "video_quality": 0, "max_height": 0,
            "ffmpeg_path": "/usr/bin/ffmpeg",
        }
        base.update(values)
        return types.SimpleNamespace(**base)

    def _asked(self, monkeypatch, streams, **values):
        from adr import vaapi

        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: streams)
        return encodingsettings.audio_language_list(
            self._config(**values), "/raw/1/title00.mkv",
        )

    def test_a_disc_with_the_language_is_left_completely_alone(self, monkeypatch):
        """The common case must not change. Someone with Swedish discs has a
        working installation and an update that started keeping English
        alongside would be a regression dressed as a fix."""
        assert self._asked(monkeypatch, self.HAS_SWEDISH) == "swe"

    def test_a_disc_without_it_asks_for_whatever_is_there(self, monkeypatch):
        assert self._asked(monkeypatch, self.ENGLISH_ONLY) == "any"

    def test_the_wildcard_is_any_and_not_und(self, monkeypatch):
        """'und' is a real language in HandBrake's table — Unknown — and
        matches only tracks tagged that way. Getting this wrong reintroduces
        the silence with a flag that looks like it fixed it."""
        assert self._asked(monkeypatch, self.ENGLISH_ONLY) != "und"
        assert encodingsettings.ANY_LANGUAGE == "any"

    def test_an_untagged_disc_still_gets_its_audio(self, monkeypatch):
        """Nothing to match against is not a reason to produce silence."""
        assert self._asked(monkeypatch, self.UNTAGGED) == "any"

    def test_an_unreadable_file_changes_nothing(self, monkeypatch):
        """Without ffprobe there is no evidence either way, and guessing
        'any' would quietly undo the language setting on every disc. The
        check after the encode is what catches this case."""
        assert self._asked(monkeypatch, []) == "swe"

    def test_no_language_wanted_means_no_flag_at_all(self, monkeypatch):
        assert self._asked(monkeypatch, self.ENGLISH_ONLY, audio_language="") == ""

    def test_a_two_letter_setting_is_matched_against_the_disc(self, monkeypatch):
        """Someone types 'sv' and the disc says 'swe'. Failing to connect
        those would fall back on a disc that has exactly what was asked for."""
        assert self._asked(monkeypatch, self.HAS_SWEDISH, audio_language="sv") == "swe"

    def test_the_flag_the_encoder_gets_carries_the_fallback(self, monkeypatch):
        """audio_language_list is only right if handbrake_overrides uses it."""
        from adr import vaapi

        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: self.ENGLISH_ONLY)
        args = encodingsettings.handbrake_overrides(
            self._config(), "/raw/1/title00.mkv",
        )
        assert args == ["--audio-lang-list", "any"]

    def test_without_a_file_the_arguments_are_unchanged(self, monkeypatch):
        """The encoder test builds a command with no source to probe, and must
        still produce the command a real encode would."""
        args = encodingsettings.handbrake_overrides(self._config())
        assert args == ["--audio-lang-list", "swe"]

    def test_the_language_can_be_read_back_off_the_arguments(self):
        overrides = ["--audio-lang-list", "any", "-q", "20"]
        assert encodingsettings.requested_language(overrides) == "any"
        assert encodingsettings.requested_language(["-q", "20"]) == ""
        assert encodingsettings.requested_language(["--audio-lang-list"]) == ""
