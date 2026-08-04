"""Encode on the GPU with ffmpeg, when HandBrake cannot.

A container can have a working Intel GPU and a HandBrake that cannot reach it:
the render node is passed through, the driver loads, vainfo lists encode
profiles, and HandBrake still says "qsv is not available on the system" for
every title of every disc, because its Quick Sync path goes through the
deprecated Media SDK rather than through VA-API.

VA-API is the same hardware by a different road, and the difference for the
person waiting is an hour per film.
"""

import stat
import subprocess
import textwrap
import types
from pathlib import Path

import pytest

from adr import vaapi


def _script(path, body):
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _config(tmp_path, **overrides):
    data = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "completed_path": tmp_path / "out",
        "vaapi_device": "/dev/dri/renderD128",
        "vaapi_codec": "h264",
        "vaapi_quality": 22,
        "vaapi_max_height": 0,
    }
    data.update(overrides)
    return types.SimpleNamespace(**data)


class TestTheCommandItBuilds:
    def _cmd(self, tmp_path, **overrides):
        return vaapi.build_command(
            "/usr/bin/ffmpeg", Path("/in.mkv"), tmp_path / "out.mp4",
            _config(tmp_path, **overrides),
        )

    def test_the_render_node_is_handed_to_ffmpeg(self, tmp_path):
        cmd = self._cmd(tmp_path)
        assert "-vaapi_device" in cmd
        assert cmd[cmd.index("-vaapi_device") + 1] == "/dev/dri/renderD128"

    def test_the_frame_is_converted_and_uploaded(self, tmp_path):
        """Without both, ffmpeg fails with "Impossible to convert between the
        formats" — a message that says nothing about hardware and sends
        people looking in the wrong place entirely."""
        chain = self._cmd(tmp_path)[self._cmd(tmp_path).index("-vf") + 1]
        assert "format=nv12" in chain
        assert "hwupload" in chain
        assert chain.index("format=nv12") < chain.index("hwupload")

    def test_the_codec_follows_the_setting(self, tmp_path):
        assert "hevc_vaapi" in self._cmd(tmp_path, vaapi_codec="hevc")
        assert "h264_vaapi" in self._cmd(tmp_path, vaapi_codec="h264")

    def test_an_unknown_codec_falls_back_rather_than_failing(self, tmp_path):
        assert "h264_vaapi" in self._cmd(tmp_path, vaapi_codec="wishful")

    def test_quality_is_clamped_to_something_sane(self, tmp_path):
        cmd = self._cmd(tmp_path, vaapi_quality=99)
        assert cmd[cmd.index("-qp") + 1] == str(vaapi.QUALITY_RANGE[1])
        cmd = self._cmd(tmp_path, vaapi_quality=1)
        assert cmd[cmd.index("-qp") + 1] == str(vaapi.QUALITY_RANGE[0])

    def test_no_height_cap_means_no_scaling(self, tmp_path):
        assert "scale" not in self._cmd(tmp_path)[self._cmd(tmp_path).index("-vf") + 1]

    def test_a_height_cap_never_upscales(self, tmp_path):
        """min(1080, ih) leaves a 720p source alone. Upscaling would cost time
        and space for a picture that is not there."""
        chain = self._cmd(tmp_path, vaapi_max_height=1080)
        chain = chain[chain.index("-vf") + 1]
        assert "min(1080,ih)" in chain.replace(" ", "")

    def test_the_first_render_node_is_found_when_none_is_configured(
        self, tmp_path, monkeypatch,
    ):
        from adr import gpu

        monkeypatch.setattr(gpu, "render_nodes", lambda: ["/dev/dri/renderD129"])
        cmd = self._cmd(tmp_path, vaapi_device="")
        assert cmd[cmd.index("-vaapi_device") + 1] == "/dev/dri/renderD129"


class TestWhatHappensToTheAudio:
    """A rip's surround track is often the reason the disc was kept."""

    def test_audio_is_copied_when_the_container_can_hold_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: ["ac3", "aac"])
        assert vaapi.audio_args("ffmpeg", Path("in.mkv"), tmp_path / "out.mp4") == [
            "-c:a", "copy",
        ]

    def test_truehd_into_mp4_is_re_encoded_rather_than_attempted(
        self, tmp_path, monkeypatch,
    ):
        """MP4 cannot hold TrueHD, and ffmpeg only finds out when it writes
        the trailer — which is to say after the entire encode."""
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: ["truehd"])
        args = vaapi.audio_args("ffmpeg", Path("in.mkv"), tmp_path / "out.mp4")
        assert args[:2] == ["-c:a", "ac3"]

    def test_dts_too(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: ["dts"])
        assert "ac3" in vaapi.audio_args("ffmpeg", Path("i.mkv"), tmp_path / "o.mp4")

    def test_one_bad_track_is_enough_to_re_encode(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: ["ac3", "dts"])
        assert "ac3" in vaapi.audio_args("ffmpeg", Path("i.mkv"), tmp_path / "o.mp4")[1]

    def test_the_fallback_keeps_surround(self, tmp_path, monkeypatch):
        """Downmixing to stereo would throw away the reason for the rip."""
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: ["truehd"])
        args = vaapi.audio_args("ffmpeg", Path("i.mkv"), tmp_path / "o.mp4")
        assert "-ac" not in args, "no downmix"
        assert "640k" in args, "enough bitrate for 5.1"

    def test_unreadable_audio_takes_the_safe_road(self, tmp_path, monkeypatch):
        """Re-encoding costs quality on a track that might have copied fine.
        The other way round costs the whole encode."""
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: [])
        assert "ac3" in vaapi.audio_args("ffmpeg", Path("i.mkv"), tmp_path / "o.mp4")

    def test_mkv_copies_anything(self, tmp_path):
        assert vaapi.audio_args("ffmpeg", Path("i.mkv"), tmp_path / "o.mkv") == [
            "-c:a", "copy",
        ]

    def test_mp4_does_not_carry_disc_subtitles(self, tmp_path):
        """PGS and VOBSUB are bitmaps and MP4 holds neither. Mapping them in
        aborts the encode at the very end."""
        cmd = vaapi.build_command(
            "ffmpeg", Path("in.mkv"), tmp_path / "out.mp4", _config(tmp_path))
        assert "0:s?" not in cmd

    def test_mkv_does_carry_them(self, tmp_path):
        cmd = vaapi.build_command(
            "ffmpeg", Path("in.mkv"), tmp_path / "out.mkv", _config(tmp_path))
        assert "0:s?" in cmd


class TestAskingWhetherItWorks:
    def test_a_missing_ffmpeg_is_reported_not_raised(self, tmp_path):
        state = vaapi.probe(_config(tmp_path, ffmpeg_path=str(tmp_path / "absent")))
        assert state["ok"] is False
        assert "not installed" in state["detail"]

    def test_a_missing_render_node_is_reported(self, tmp_path):
        state = vaapi.probe(_config(
            tmp_path, ffmpeg_path="/bin/true",
            vaapi_device=str(tmp_path / "no-such-node"),
        ))
        assert state["ok"] is False
        assert "does not exist" in state["detail"]

    def test_a_working_gpu_lists_the_codecs_that_encoded(self, tmp_path, monkeypatch):
        node = tmp_path / "renderD128"
        node.write_text("")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout="", stderr=""))
        state = vaapi.probe(_config(
            tmp_path, ffmpeg_path="/bin/true", vaapi_device=str(node)))
        assert state["ok"] is True
        assert set(state["codecs"]) == {"h264", "hevc"}

    def test_a_gpu_that_only_does_h264_says_so(self, tmp_path, monkeypatch):
        node = tmp_path / "renderD128"
        node.write_text("")

        def run(cmd, *a, **k):
            failed = "hevc_vaapi" in cmd
            return types.SimpleNamespace(
                returncode=1 if failed else 0, stdout="",
                stderr="No support for codec hevc profile 1\n" if failed else "")

        monkeypatch.setattr(subprocess, "run", run)
        state = vaapi.probe(_config(
            tmp_path, ffmpeg_path="/bin/true", vaapi_device=str(node)))
        assert state["ok"] is True
        assert state["codecs"] == ["h264"]

    def test_a_gpu_that_encodes_nothing_explains_itself(self, tmp_path, monkeypatch):
        """Reporting failure without ffmpeg's own words leaves someone with a
        red cross and nowhere to go."""
        node = tmp_path / "renderD128"
        node.write_text("")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(
            returncode=1, stdout="", stderr="Failed to initialise VAAPI connection\n"))
        state = vaapi.probe(_config(
            tmp_path, ffmpeg_path="/bin/true", vaapi_device=str(node)))
        assert state["ok"] is False
        assert "Failed to initialise VAAPI connection" in state["detail"]

    def test_it_probes_by_encoding_not_by_reading_a_list(self, tmp_path, monkeypatch):
        """ffmpeg lists h264_vaapi on any build compiled with VA-API, with or
        without a GPU. That answers a different question from the one asked."""
        node = tmp_path / "renderD128"
        node.write_text("")
        seen = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: (
            seen.append(cmd), types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1])
        vaapi.probe(_config(tmp_path, ffmpeg_path="/bin/true", vaapi_device=str(node)))
        assert seen, "it ran something"
        for cmd in seen:
            assert "-encoders" not in cmd
            assert "lavfi" in " ".join(cmd), "it generates frames and encodes them"


class TestReadingHowLongTheFilmIs:
    """Without a duration there is no denominator, and the bar cannot move."""

    def test_ffprobe_answers_in_one_number(self, tmp_path, monkeypatch):
        _script(tmp_path / "ffprobe", "echo 5025.4\n")
        assert vaapi.probe_duration(str(tmp_path / "ffmpeg"), tmp_path) == pytest.approx(5025.4)

    def test_ffmpegs_banner_is_the_fallback(self, tmp_path):
        exe = _script(tmp_path / "ffmpeg",
                      'echo "  Duration: 01:23:45.60, start: 0.000" >&2\n')
        assert vaapi.probe_duration(exe, tmp_path) == pytest.approx(5025.6)

    def test_nothing_readable_is_zero_rather_than_an_error(self, tmp_path):
        exe = _script(tmp_path / "ffmpeg", "echo nothing useful\n")
        assert vaapi.probe_duration(exe, tmp_path) == 0.0


class TestEncodingForReal:
    def _ffmpeg(self, tmp_path, body):
        return _script(tmp_path / "ffmpeg", body)

    def test_a_successful_encode_reports_the_output(self, tmp_path):
        exe = self._ffmpeg(tmp_path, """
            for last; do :; done
            printf video > "$last"
        """)
        source = tmp_path / "in.mkv"
        source.write_text("x")
        encoder = vaapi.VaapiEncoder(_config(tmp_path, ffmpeg_path=exe))
        result = encoder.encode(source, output_dir=tmp_path / "out")
        assert result.success is True
        assert result.output_path.exists()
        assert result.output_path.name == "in.mp4"

    def test_a_failure_carries_ffmpegs_own_words(self, tmp_path):
        """"exited with code 1" on its own sends someone to a log to find the
        one line that mattered."""
        exe = self._ffmpeg(tmp_path, """
            echo "Error initialising VAAPI: no such device" >&2
            exit 1
        """)
        source = tmp_path / "in.mkv"
        source.write_text("x")
        encoder = vaapi.VaapiEncoder(_config(tmp_path, ffmpeg_path=exe))
        result = encoder.encode(source, output_dir=tmp_path / "out")
        assert result.success is False
        assert "no such device" in result.error

    def test_an_encode_that_writes_nothing_is_not_a_success(self, tmp_path):
        exe = self._ffmpeg(tmp_path, "exit 0\n")
        source = tmp_path / "in.mkv"
        source.write_text("x")
        encoder = vaapi.VaapiEncoder(_config(tmp_path, ffmpeg_path=exe))
        result = encoder.encode(source, output_dir=tmp_path / "out")
        assert result.success is False

    def test_a_missing_input_is_caught_before_anything_runs(self, tmp_path):
        encoder = vaapi.VaapiEncoder(_config(tmp_path, ffmpeg_path="/bin/false"))
        result = encoder.encode(tmp_path / "gone.mkv")
        assert result.success is False
        assert "not found" in result.error

    def test_a_subfolder_in_the_name_is_created(self, tmp_path):
        """Extras go in "Other/", one of the names Plex recognises."""
        exe = self._ffmpeg(tmp_path, 'for last; do :; done\nprintf v > "$last"\n')
        source = tmp_path / "in.mkv"
        source.write_text("x")
        encoder = vaapi.VaapiEncoder(_config(tmp_path, ffmpeg_path=exe))
        result = encoder.encode(
            source, output_dir=tmp_path / "out", output_filename="Other/Trailer")
        assert result.success is True
        assert result.output_path.parent.name == "Other"

    def test_progress_reaches_the_callback(self, tmp_path, monkeypatch):
        exe = self._ffmpeg(tmp_path, """
            for last; do :; done
            printf video > "$last"
            echo "out_time_us=30000000"
            echo "fps=118.0"
            echo "progress=continue"
        """)
        monkeypatch.setattr(vaapi, "probe_duration", lambda exe, path: 60.0)
        source = tmp_path / "in.mkv"
        source.write_text("x")
        seen = []
        encoder = vaapi.VaapiEncoder(_config(tmp_path, ffmpeg_path=exe))
        encoder.encode(source, output_dir=tmp_path / "out",
                       progress_callback=seen.append)
        assert seen, "the callback was called"
        assert seen[-1]["progress"] == pytest.approx(0.5)
        assert seen[-1]["fps"] == pytest.approx(118.0)
        assert seen[-1]["state"] == "working"

    def test_an_unknown_duration_reports_no_fraction_rather_than_a_made_up_one(
        self, tmp_path, monkeypatch,
    ):
        exe = self._ffmpeg(tmp_path, """
            for last; do :; done
            printf video > "$last"
            echo "out_time_us=30000000"
            echo "progress=continue"
        """)
        monkeypatch.setattr(vaapi, "probe_duration", lambda exe, path: 0.0)
        source = tmp_path / "in.mkv"
        source.write_text("x")
        seen = []
        encoder = vaapi.VaapiEncoder(_config(tmp_path, ffmpeg_path=exe))
        encoder.encode(source, output_dir=tmp_path / "out",
                       progress_callback=seen.append)
        assert all(p["progress"] == 0.0 for p in seen)

    def test_the_log_sink_sees_what_ffmpeg_said(self, tmp_path):
        """The whole point of the job log: a failure explains itself in the UI
        rather than in journalctl."""
        exe = self._ffmpeg(tmp_path, 'echo "something went wrong" >&2\nexit 1\n')
        source = tmp_path / "in.mkv"
        source.write_text("x")
        lines = []
        encoder = vaapi.VaapiEncoder(_config(tmp_path, ffmpeg_path=exe))
        encoder.log_sink = lines.append
        encoder.encode(source, output_dir=tmp_path / "out")
        assert any("something went wrong" in line for line in lines)

    def test_it_registers_for_cancellation(self, tmp_path):
        """A rip that cannot be stopped is a rip that owns the machine."""
        exe = self._ffmpeg(tmp_path, 'for last; do :; done\nprintf v > "$last"\n')
        source = tmp_path / "in.mkv"
        source.write_text("x")

        registered, unregistered = [], []
        registry = types.SimpleNamespace(
            register=lambda job, proc: registered.append(job),
            unregister=lambda job, proc: unregistered.append(job),
        )
        encoder = vaapi.VaapiEncoder(_config(tmp_path, ffmpeg_path=exe))
        encoder._process_registry = registry
        encoder.encode(source, output_dir=tmp_path / "out", job_id=7)
        assert registered == [7]
        assert unregistered == [7]


class TestChoosingTheBackend:
    def test_the_default_is_handbrake(self, tmp_path):
        from adr.encoder import HandBrakeEncoder
        from adr.encoderfactory import build_encoder

        config = types.SimpleNamespace(
            encoder_backend="handbrake",
            handbrake_path="/bin/true", handbrake_preset="Fast 1080p30",
            handbrake_preset_file="", handbrake_extra_args="",
            completed_path=tmp_path,
        )
        assert isinstance(build_encoder(config), HandBrakeEncoder)

    def test_vaapi_gets_the_ffmpeg_encoder(self, tmp_path):
        from adr.encoderfactory import build_encoder

        config = _config(tmp_path, encoder_backend="vaapi")
        assert isinstance(build_encoder(config), vaapi.VaapiEncoder)

    def test_an_unknown_backend_falls_back_rather_than_stopping_everything(self):
        """A typo in the config file is a reason to use the default, not to
        leave every disc stuck in the queue."""
        from adr.config import Config

        config = Config.__new__(Config)
        config._data = {"encoder_backend": "gpu-please"}
        assert config.encoder_backend == "handbrake"

    def test_the_backend_describes_itself_for_the_ui(self, tmp_path):
        from adr.encoderfactory import describe_backend

        config = _config(tmp_path, encoder_backend="vaapi", vaapi_codec="hevc")
        assert "HEVC" in describe_backend(config)
        assert "GPU" in describe_backend(config)


class TestSwitchingWhileRunning:
    """A setting that appears to take effect and does not is worse than one
    that refuses to change — and this application has spent long enough on
    exactly that failure."""

    def _worker(self, tmp_path, backend="handbrake"):
        import queue as queue_mod

        from adr.pipeline import EncoderWorker

        config = types.SimpleNamespace(
            encoder_backend=backend,
            handbrake_path="/bin/true", handbrake_preset="Fast 1080p30",
            handbrake_preset_file="", handbrake_extra_args="",
            completed_path=tmp_path, ffmpeg_path="/bin/true",
            vaapi_device="", vaapi_codec="h264", vaapi_quality=22,
            vaapi_max_height=0,
        )
        return config, EncoderWorker(config, queue_mod.Queue())

    def test_a_backend_change_is_picked_up_without_a_restart(self, tmp_path):
        from adr.encoder import HandBrakeEncoder

        config, worker = self._worker(tmp_path)
        assert isinstance(worker._current_encoder(), HandBrakeEncoder)

        config.encoder_backend = "vaapi"
        assert isinstance(worker._current_encoder(), vaapi.VaapiEncoder)

    def test_an_unchanged_backend_keeps_the_same_encoder(self, tmp_path):
        """Rebuilding per job would drop HandBrake's preset discovery on the
        floor every time, for nothing."""
        _, worker = self._worker(tmp_path)
        assert worker._current_encoder() is worker._current_encoder()

    def test_the_rebuilt_encoder_can_still_be_cancelled(self, tmp_path):
        """A rip that cannot be stopped owns the machine until it finishes."""
        config, worker = self._worker(tmp_path)
        config.encoder_backend = "vaapi"
        assert worker._current_encoder()._process_registry is not None
