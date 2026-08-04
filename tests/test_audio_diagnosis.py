"""Why the film came out in the wrong language.

"Still in English" has several causes that look identical from the outside:
nothing was asked for, the disc tags no languages so nothing can be matched,
the language asked for is not on this disc, or HandBrake's preset decided.
They need different things done about them — set the language, accept the
disc, check what it holds, edit the preset — and until this existed there was
nothing on screen to tell them apart.
"""

import types

from adr import vaapi
from adr.encoder import describe_audio_request

SWEDISH_DISC = [
    {"codec": "ac3", "language": "eng"},
    {"codec": "ac3", "language": "swe"},
]
UNTAGGED_DISC = [
    {"codec": "ac3", "language": ""},
    {"codec": "ac3", "language": ""},
]


class TestTheGpuPathExplainsItself:
    def test_it_names_the_track_it_chose(self):
        said = vaapi.describe_audio_choice(SWEDISH_DISC, "swe")
        assert "Track 1" in said and "swe" in said

    def test_it_lists_every_track_and_its_language(self):
        """So the answer can be checked rather than taken on trust."""
        said = vaapi.describe_audio_choice(SWEDISH_DISC, "swe")
        assert "0:eng" in said and "1:swe" in said

    def test_a_disc_with_no_language_tags_is_named_as_the_cause(self):
        """The one nobody can guess: the setting is right, the code is right,
        and there is nothing on the disc to match it against."""
        said = vaapi.describe_audio_choice(UNTAGGED_DISC, "swe")
        assert "None of them carries a language tag" in said
        assert "not a setting" in said

    def test_an_untagged_track_is_shown_as_untagged_not_blank(self):
        said = vaapi.describe_audio_choice(UNTAGGED_DISC, "swe")
        assert "untagged" in said

    def test_no_language_set_says_so_and_says_where_to_set_it(self):
        said = vaapi.describe_audio_choice(SWEDISH_DISC, "")
        assert "No spoken language is set" in said
        assert "Settings" in said

    def test_a_language_the_disc_lacks_is_distinguished_from_an_untagged_disc(self):
        """Two different problems: one is the disc, one is the choice."""
        said = vaapi.describe_audio_choice(SWEDISH_DISC, "jpn")
        assert "None is 'jpn'" in said
        assert "carries a language tag" not in said

    def test_a_file_it_could_not_read_says_that_rather_than_guessing(self):
        assert "nothing could be read" in vaapi.describe_audio_choice([], "swe")


class TestHandBrakeExplainsItselfToo:
    def _config(self, **values):
        base = {
            "audio_language": "", "video_quality": 0, "max_height": 0,
            "handbrake_preset": "Super HQ 1080p30 Surround (Svenska)",
        }
        base.update(values)
        return types.SimpleNamespace(**base)

    def test_it_shows_the_flags_it_actually_passes(self):
        """The preset is a thousand-line JSON file nobody opens mid-encode."""
        said = describe_audio_request(self._config(audio_language="swe"))
        assert "--audio-lang-list swe" in said

    def test_it_translates_a_two_letter_code_the_way_the_encoder_will(self):
        said = describe_audio_request(self._config(audio_language="sv"))
        assert "'swe'" in said

    def test_with_no_setting_the_preset_s_own_language_is_used(self):
        """The shipped preset asks for Swedish. Leaving the setting blank must
        not mean English — the preset is the template, and it named one."""
        said = describe_audio_request(self._config())
        assert "'swe'" in said
        assert "Super HQ 1080p30 Surround (Svenska)" in said
        assert "Settings" in said

    def test_with_nothing_named_anywhere_the_disc_decides(self):
        said = describe_audio_request(self._config(handbrake_preset="Fast 1080p30"))
        assert "the disc's own track order" in said
        assert "Settings" in said

    def test_it_says_the_preset_still_decides_how_many_tracks(self):
        """Setting the language does not override
        AudioTrackSelectionBehavior, and someone expecting every language
        would otherwise read the log as a contradiction."""
        said = describe_audio_request(self._config(audio_language="swe"))
        assert "AudioTrackSelectionBehavior" in said


class TestItReachesTheJobLog:
    """A diagnosis in the service log is one `pct exec` away; in the job's own
    log it is on the History page beside the film it is about."""

    def test_the_gpu_encoder_writes_it(self, tmp_path, monkeypatch):
        import stat
        import textwrap

        exe = tmp_path / "ffmpeg"
        exe.write_text(textwrap.dedent('''\
            #!/bin/sh
            for last; do :; done
            printf video > "$last"
        '''))
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        source = tmp_path / "in.mkv"
        source.write_bytes(b"x")

        monkeypatch.setattr(vaapi, "audio_streams", lambda e, p: SWEDISH_DISC)
        config = types.SimpleNamespace(
            ffmpeg_path=str(exe), completed_path=tmp_path / "out",
            vaapi_device="/dev/dri/renderD128", vaapi_codec="h264",
            video_quality=22, max_height=0, audio_language="swe",
        )
        lines = []
        encoder = vaapi.VaapiEncoder(config)
        encoder.log_sink = lines.append
        encoder.encode(source, output_dir=tmp_path / "out")
        assert any("Audio:" in line for line in lines), lines

    def test_handbrake_writes_it(self, tmp_path, monkeypatch):
        import stat
        import textwrap

        from adr.encoder import HandBrakeEncoder

        exe = tmp_path / "hb"
        exe.write_text(textwrap.dedent('''\
            #!/bin/sh
            for a in "$@"; do
              if [ "$prev" = "-o" ]; then printf video > "$a"; fi
              prev="$a"
            done
        '''))
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        source = tmp_path / "in.mkv"
        source.write_bytes(b"x")

        config = types.SimpleNamespace(
            handbrake_path=str(exe), handbrake_preset="Fast 1080p30",
            handbrake_preset_file="", handbrake_extra_args="",
            completed_path=tmp_path / "out", audio_language="swe",
            video_quality=0, max_height=0, libva_driver="",
        )
        lines = []
        encoder = HandBrakeEncoder(config)
        encoder.log_sink = lines.append
        encoder.encode(source, output_dir=tmp_path / "out")
        assert any("Audio:" in line for line in lines), lines
