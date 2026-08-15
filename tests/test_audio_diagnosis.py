"""Why the film came out in the wrong language.

"Still in English" has several causes that look identical from the outside:
nothing was asked for, the disc tags no languages so nothing can be matched,
the language asked for is not on this disc, or HandBrake's preset decided.
They need different things done about them — set the language, accept the
disc, check what it holds, edit the preset — and until this existed there was
nothing on screen to tell them apart.
"""

import types
from pathlib import Path

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


class TestASilentFilmIsNotASuccess:
    """An encoder can drop every audio track and still exit 0.

    HandBrake does it whenever the language list matched nothing, and twice
    more besides — an Auto Passthru that could not be satisfied, a mixdown at
    a samplerate the encoder will not take. All three log a line and carry on.
    The film plays perfectly, in silence, and the job says Done; the only
    thing that notices is somebody sitting down to watch it a week later,
    with the raw files long cleaned up and the disc back on the shelf.
    """

    def _files(self, tmp_path):
        source = tmp_path / "in.mkv"
        source.write_bytes(b"x")
        produced = tmp_path / "out.mp4"
        produced.write_bytes(b"x")
        return source, produced

    def test_audio_in_and_none_out_is_a_complaint(self, tmp_path, monkeypatch):
        source, produced = self._files(tmp_path)
        monkeypatch.setattr(
            vaapi, "audio_streams",
            lambda exe, path: SWEDISH_DISC if path.suffix == ".mkv" else [],
        )
        monkeypatch.setattr(vaapi, "probe_duration", lambda exe, path: 5400.0)
        said = vaapi.audio_went_missing("/usr/bin/ffmpeg", source, produced)
        assert "no audio at all" in said
        assert "in.mkv" in said and "out.mp4" in said

    def test_it_says_what_to_do_about_it(self, tmp_path, monkeypatch):
        """A failure nobody can act on is only half reported."""
        source, produced = self._files(tmp_path)
        monkeypatch.setattr(
            vaapi, "audio_streams",
            lambda exe, path: SWEDISH_DISC if path.suffix == ".mkv" else [],
        )
        monkeypatch.setattr(vaapi, "probe_duration", lambda exe, path: 5400.0)
        said = vaapi.audio_went_missing("/usr/bin/ffmpeg", source, produced)
        assert "Retry" in said and "disc is not needed" in said

    def test_a_source_that_never_had_sound_is_not_a_fault(self, tmp_path, monkeypatch):
        """Refusing to finish a genuinely silent disc would be a new bug
        replacing the old one."""
        source, produced = self._files(tmp_path)
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: [])
        assert vaapi.audio_went_missing("/usr/bin/ffmpeg", source, produced) == ""

    def test_audio_that_survived_says_nothing(self, tmp_path, monkeypatch):
        source, produced = self._files(tmp_path)
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: SWEDISH_DISC)
        assert vaapi.audio_went_missing("/usr/bin/ffmpeg", source, produced) == ""

    def test_a_missing_file_is_not_answered_with_a_guess(self, tmp_path, monkeypatch):
        """This exists to catch a definite loss, not to block an encode over a
        question it could not answer."""
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: SWEDISH_DISC)
        assert vaapi.audio_went_missing(
            "/usr/bin/ffmpeg", tmp_path / "gone.mkv", tmp_path / "gone.mp4",
        ) == ""

    def test_no_ffprobe_lets_the_encode_stand(self, tmp_path, monkeypatch):
        """audio_streams returns [] when it cannot run at all, and an
        installation without ffprobe must not fail every encode."""
        source, produced = self._files(tmp_path)
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: [])
        assert vaapi.audio_went_missing("/usr/bin/ffmpeg", source, produced) == ""


class TestTheEncodersRefuseToCallItDone:
    """The check is only worth having if it reaches the result."""

    def _handbrake(self, tmp_path):
        import stat
        import textwrap

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
            ffmpeg_path="/usr/bin/ffmpeg",
        )
        return config, source

    def test_handbrake_exiting_0_with_no_audio_is_a_failure(self, tmp_path, monkeypatch):
        from adr.encoder import HandBrakeEncoder

        config, source = self._handbrake(tmp_path)
        monkeypatch.setattr(
            vaapi, "audio_streams",
            lambda exe, path: SWEDISH_DISC if path.suffix == ".mkv" else [],
        )
        monkeypatch.setattr(vaapi, "probe_duration", lambda exe, path: 5400.0)
        result = HandBrakeEncoder(config).encode(source, output_dir=tmp_path / "out")
        assert result.success is False
        assert "no audio at all" in result.error

    def test_it_says_so_in_the_job_s_own_log(self, tmp_path, monkeypatch):
        """Not journalctl. The History page, beside the film it is about."""
        from adr.encoder import HandBrakeEncoder

        config, source = self._handbrake(tmp_path)
        monkeypatch.setattr(
            vaapi, "audio_streams",
            lambda exe, path: SWEDISH_DISC if path.suffix == ".mkv" else [],
        )
        monkeypatch.setattr(vaapi, "probe_duration", lambda exe, path: 5400.0)
        lines = []
        encoder = HandBrakeEncoder(config)
        encoder.log_sink = lines.append
        encoder.encode(source, output_dir=tmp_path / "out")
        assert any("no audio at all" in line for line in lines), lines

    def test_a_normal_encode_still_succeeds(self, tmp_path, monkeypatch):
        from adr.encoder import HandBrakeEncoder

        config, source = self._handbrake(tmp_path)
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: SWEDISH_DISC)
        result = HandBrakeEncoder(config).encode(source, output_dir=tmp_path / "out")
        assert result.success is True
        assert result.error is None


class TestTheLogNamesTheDiscThatCannotAnswer:
    def _config(self, tmp_path, **values):
        base = {
            "audio_language": "swe", "video_quality": 0, "max_height": 0,
            "handbrake_preset": "Super HQ 1080p30 Surround (Svenska)",
            "ffmpeg_path": "/usr/bin/ffmpeg",
        }
        base.update(values)
        return types.SimpleNamespace(**base)

    def test_it_says_the_language_is_not_on_this_disc(self, tmp_path, monkeypatch):
        english = [{"codec": "ac3", "language": "eng"}]
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: english)
        said = describe_audio_request(self._config(tmp_path), tmp_path / "in.mkv")
        assert "has no track in that language" in said
        assert "0:eng" in said
        assert "'any'" in said

    def test_it_no_longer_claims_a_fallback_that_does_not_exist(self, tmp_path, monkeypatch):
        """This line used to end "if the disc has no track in that language
        the preset falls back to its own rules". HandBrake has no such rule,
        and the log said everything was fine while the film came out mute."""
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: SWEDISH_DISC)
        said = describe_audio_request(self._config(tmp_path), tmp_path / "in.mkv")
        assert "falls back to its own rules" not in said


class TestTheCheckDoesNotInventFailures:
    """Every guard below exists because a reviewer broke the first version.

    The check answers "did the audio vanish" by asking ffprobe twice. Its
    trouble is that ``audio_streams`` gives the same empty list for "no audio"
    and for "could not look", so read carelessly it accuses any file it cannot
    open — and the file it cannot open is most often the *output*, written
    straight to a NAS with a sixty-second probe timeout.
    """

    def _files(self, tmp_path):
        source = tmp_path / "in.mkv"
        source.write_bytes(b"x")
        produced = tmp_path / "out.mp4"
        produced.write_bytes(b"x")
        return source, produced

    def test_an_unreadable_output_is_not_called_silent(self, tmp_path, monkeypatch):
        """The regression this class is named for. Source probes fine, output
        does not — a timeout, a container ffprobe does not know — and the
        first version failed a perfectly good encode for it."""
        source, produced = self._files(tmp_path)
        monkeypatch.setattr(
            vaapi, "audio_streams",
            lambda exe, path: SWEDISH_DISC if path.suffix == ".mkv" else [],
        )
        monkeypatch.setattr(vaapi, "probe_duration", lambda exe, path: 0.0)
        assert vaapi.audio_went_missing("/usr/bin/ffmpeg", source, produced) == ""

    def test_a_duration_is_what_makes_an_empty_answer_mean_something(
        self, tmp_path, monkeypatch,
    ):
        source, produced = self._files(tmp_path)
        monkeypatch.setattr(
            vaapi, "audio_streams",
            lambda exe, path: SWEDISH_DISC if path.suffix == ".mkv" else [],
        )
        monkeypatch.setattr(vaapi, "probe_duration", lambda exe, path: 1.0)
        assert "no audio at all" in vaapi.audio_went_missing(
            "/usr/bin/ffmpeg", source, produced)

    def test_a_caller_that_already_probed_is_not_charged_twice(
        self, tmp_path, monkeypatch,
    ):
        """One encode used to spawn ffprobe three times for one answer."""
        source, produced = self._files(tmp_path)
        calls = []

        def counted(exe, path):
            calls.append(path)
            return []

        monkeypatch.setattr(vaapi, "audio_streams", counted)
        monkeypatch.setattr(vaapi, "probe_duration", lambda exe, path: 5400.0)
        vaapi.audio_went_missing(
            "/usr/bin/ffmpeg", source, produced, source_streams=SWEDISH_DISC)
        assert [p.suffix for p in calls] == [".mp4"], "the source was probed again"


class TestOneQuietExtraDoesNotStrandTheFilm:
    """Extras are ordinary tracks of the same job, and one ERROR track fails
    the job — which leaves the *film* in staging, never transferred and never
    announced, because a ninety-second featurette came out quiet."""

    def _encoder(self, tmp_path, monkeypatch):
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
            ffmpeg_path="/usr/bin/ffmpeg",
        )
        monkeypatch.setattr(
            vaapi, "audio_streams",
            lambda exe, path: SWEDISH_DISC if path.suffix == ".mkv" else [],
        )
        monkeypatch.setattr(vaapi, "probe_duration", lambda exe, path: 90.0)
        return HandBrakeEncoder(config), source

    def test_a_silent_extra_still_counts_as_done(self, tmp_path, monkeypatch):
        encoder, source = self._encoder(tmp_path, monkeypatch)
        result = encoder.encode(
            source, output_dir=tmp_path / "out", output_filename="Other/Extra 1")
        assert result.success is True

    def test_but_it_is_said_out_loud(self, tmp_path, monkeypatch):
        encoder, source = self._encoder(tmp_path, monkeypatch)
        lines = []
        encoder.log_sink = lines.append
        encoder.encode(
            source, output_dir=tmp_path / "out", output_filename="Other/Extra 1")
        assert any("no audio at all" in line for line in lines), lines
        assert any("not held up" in line for line in lines), lines

    def test_the_main_feature_is_still_a_failure(self, tmp_path, monkeypatch):
        encoder, source = self._encoder(tmp_path, monkeypatch)
        result = encoder.encode(
            source, output_dir=tmp_path / "out", output_filename="Jumanji (1995)")
        assert result.success is False

    def test_the_silent_film_is_taken_away(self, tmp_path, monkeypatch):
        """It is a complete, playable file under the name the finished film
        should have. Left there, the retry finds the name taken and writes the
        good encode to "Jumanji (1995) (2)" — a misnamed library folder, and a
        silent copy no row points at and no cleanup can find."""
        encoder, source = self._encoder(tmp_path, monkeypatch)
        result = encoder.encode(
            source, output_dir=tmp_path / "out", output_filename="Jumanji (1995)")
        assert not Path(result.output_path).exists()

    def test_an_extra_that_kept_its_audio_is_untouched(self, tmp_path, monkeypatch):
        encoder, source = self._encoder(tmp_path, monkeypatch)
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: SWEDISH_DISC)
        result = encoder.encode(
            source, output_dir=tmp_path / "out", output_filename="Other/Extra 1")
        assert result.success is True
        assert Path(result.output_path).exists()


class TestTheEncoderActuallyPassesTheFile:
    """The load-bearing line of the whole fix, and it had no test at all.

    Deleting ``input_path`` from the handbrake_overrides call in encode()
    reverts the pre-encode half of this — films go back to being encoded
    silent — and the entire suite still passed. So this asserts on the real
    argv, from a real subprocess, rather than on anything in between.
    """

    def _run(self, tmp_path, monkeypatch, streams, **overrides):
        import stat
        import textwrap

        from adr.encoder import HandBrakeEncoder

        argv = tmp_path / "argv.txt"
        exe = tmp_path / "hb"
        exe.write_text(textwrap.dedent(f'''\
            #!/bin/sh
            for a in "$@"; do
              printf '%s\\n' "$a" >> "{argv}"
              if [ "$prev" = "-o" ]; then printf video > "$a"; fi
              prev="$a"
            done
        '''))
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        source = tmp_path / "in.mkv"
        source.write_bytes(b"x")

        base = dict(
            handbrake_path=str(exe), handbrake_preset="Fast 1080p30",
            handbrake_preset_file="", handbrake_extra_args="",
            completed_path=tmp_path / "out", audio_language="swe",
            video_quality=0, max_height=0, libva_driver="",
            ffmpeg_path="/usr/bin/ffmpeg",
        )
        base.update(overrides)
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: streams)
        monkeypatch.setattr(vaapi, "probe_duration", lambda exe, path: 5400.0)
        HandBrakeEncoder(types.SimpleNamespace(**base)).encode(
            source, output_dir=tmp_path / "out")
        return argv.read_text().splitlines() if argv.exists() else []

    def test_an_english_only_disc_reaches_handbrake_as_any(self, tmp_path, monkeypatch):
        args = self._run(tmp_path, monkeypatch, [{"codec": "ac3", "language": "eng"}])
        assert "--audio-lang-list" in args
        assert args[args.index("--audio-lang-list") + 1] == "any"

    def test_a_swedish_disc_still_reaches_it_as_swedish(self, tmp_path, monkeypatch):
        args = self._run(tmp_path, monkeypatch, SWEDISH_DISC)
        assert args[args.index("--audio-lang-list") + 1] == "swe"


class TestHandWrittenArgumentsAreReportedHonestly:
    """handbrake_extra_args is appended after the overrides so that it wins.
    A log line reading only the overrides would confidently name a language
    that never reached HandBrake, and point away from the setting that did."""

    def test_the_last_one_is_the_one_that_counts(self):
        from adr import encodingsettings

        assert encodingsettings.requested_language(
            ["--audio-lang-list", "any", "-q", "20", "--audio-lang-list", "fre"],
        ) == "fre"

    def test_a_command_without_one_says_nothing(self):
        from adr import encodingsettings

        assert encodingsettings.requested_language(["-q", "20"]) == ""
        assert encodingsettings.requested_language(["--audio-lang-list"]) == ""
