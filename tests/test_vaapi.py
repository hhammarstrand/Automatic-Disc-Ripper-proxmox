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
    """A film that plays silently is worse than one that fails to encode.

    Copying the disc's AC-3 into an MP4 and stopping there is legal and is
    what a lot of hardware will not decode — a TV, a phone, a browser. The
    film plays with no sound and nothing anywhere says why. HandBrake's
    "Surround" presets put an AAC stereo track first for exactly this reason,
    and that is the shape mirrored here.
    """

    def _plan(self, tmp_path, monkeypatch, codecs, suffix=".mp4"):
        monkeypatch.setattr(vaapi, "audio_streams", lambda exe, path: codecs)
        return vaapi.audio_plan("ffmpeg", Path("in.mkv"), tmp_path / f"out{suffix}")

    def test_a_stereo_track_comes_first(self, tmp_path, monkeypatch):
        """The guarantee that something comes out of the speakers."""
        plan = self._plan(tmp_path, monkeypatch, ["ac3"])
        assert plan[plan.index("-c:a:0") + 1] == "aac"
        assert plan[plan.index("-ac:a:0") + 1] == "2"

    def test_the_stereo_track_is_the_default_one(self, tmp_path, monkeypatch):
        """Otherwise the player picks whichever track the file lists first,
        which on a multi-language disc is a coin toss over the language."""
        plan = self._plan(tmp_path, monkeypatch, ["ac3", "ac3"])
        assert plan[plan.index("-disposition:a:0") + 1] == "default"

    def test_the_surround_track_is_kept_alongside_it(self, tmp_path, monkeypatch):
        """Surround is usually the reason the disc was kept."""
        plan = self._plan(tmp_path, monkeypatch, ["ac3"])
        assert plan[plan.index("-c:a:1") + 1] == "copy"

    def test_every_source_track_survives(self, tmp_path, monkeypatch):
        """A Swedish disc carries Swedish and English. Picking for the user
        would be choosing which language they are allowed."""
        plan = self._plan(tmp_path, monkeypatch, ["ac3", "ac3", "ac3"])
        assert "-c:a:1" in plan and "-c:a:2" in plan and "-c:a:3" in plan

    def test_truehd_becomes_ac3_rather_than_failing_at_the_end(
        self, tmp_path, monkeypatch,
    ):
        """MP4 cannot hold TrueHD, and ffmpeg only finds out when it writes
        the trailer — which is to say after the entire encode."""
        plan = self._plan(tmp_path, monkeypatch, ["truehd"])
        assert plan[plan.index("-c:a:1") + 1] == "ac3"
        assert plan[plan.index("-b:a:1") + 1] == "640k"

    def test_dts_too(self, tmp_path, monkeypatch):
        plan = self._plan(tmp_path, monkeypatch, ["dts"])
        assert plan[plan.index("-c:a:1") + 1] == "ac3"

    def test_each_track_is_judged_on_its_own(self, tmp_path, monkeypatch):
        """One DTS track is no reason to re-encode the AC-3 next to it."""
        plan = self._plan(tmp_path, monkeypatch, ["ac3", "dts"])
        assert plan[plan.index("-c:a:1") + 1] == "copy"
        assert plan[plan.index("-c:a:2") + 1] == "ac3"

    def test_the_fallback_keeps_surround(self, tmp_path, monkeypatch):
        """640k is enough for 5.1; downmixing it would throw away the reason
        for the rip. Only the added stereo track is two channels."""
        plan = self._plan(tmp_path, monkeypatch, ["truehd"])
        assert "-ac:a:1" not in plan, "the surround track keeps its channels"

    def test_unreadable_audio_takes_the_safe_road(self, tmp_path, monkeypatch):
        """Re-encoding costs quality on a track that might have copied fine.
        The other way round costs the whole encode."""
        plan = self._plan(tmp_path, monkeypatch, [])
        assert "ac3" in plan

    def test_mkv_copies_anything(self, tmp_path, monkeypatch):
        """MKV holds TrueHD, DTS-HD and PGS, so nothing has to be decided."""
        plan = self._plan(tmp_path, monkeypatch, ["truehd"], suffix=".mkv")
        assert plan == ["-map", "0:a?", "-c:a", "copy"]

    def test_the_plan_brings_its_own_maps(self, tmp_path, monkeypatch):
        """How many output tracks there are depends on the source, so the
        mapping and the codecs have to be decided together or they drift."""
        plan = self._plan(tmp_path, monkeypatch, ["ac3"])
        assert plan[:4] == ["-map", "0:a:0", "-map", "0:a?"]

    def test_the_command_does_not_map_audio_twice(self, tmp_path):
        """A second -map 0:a? in build_command would duplicate every track."""
        cmd = vaapi.build_command(
            "ffmpeg", Path("in.mkv"), tmp_path / "out.mp4", _config(tmp_path),
            ["-map", "0:a:0", "-map", "0:a?", "-c:a:0", "aac"])
        assert cmd.count("0:a?") == 1

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


class TestTestingWhatWillActuallyRun:
    """A red cross about a HandBrake preset that nothing is going to use —
    because encoding moved to the GPU — is the same class of wrong answer
    this page exists to prevent."""

    def _config(self, tmp_path, **overrides):
        base = {
            "encoder_backend": "vaapi",
            "handbrake_path": str(tmp_path / "no-handbrake"),
            "handbrake_preset": "Super HQ 1080p30 Surround (Svenska)",
            "handbrake_preset_file": "", "handbrake_extra_args": "",
            "ffmpeg_path": "/bin/true", "completed_path": tmp_path,
            "vaapi_device": "/dev/dri/renderD128", "vaapi_codec": "h264",
            "vaapi_quality": 22, "vaapi_max_height": 0,
        }
        base.update(overrides)
        return types.SimpleNamespace(**base)

    def test_the_gpu_backend_is_not_judged_by_handbrake(self, tmp_path, monkeypatch):
        from adr import encodertest

        monkeypatch.setattr(vaapi, "probe", lambda config: {
            "ok": True, "codecs": ["h264"], "detail": "ffmpeg encoded a clip."})
        result = encodertest.test_encoder(self._config(tmp_path))
        names = [s["name"] for s in result["steps"]]
        assert "Preset" not in names, "HandBrake's preset is irrelevant here"
        assert "Encoder" in names and "GPU" in names

    def test_a_missing_handbrake_does_not_fail_a_gpu_setup(self, tmp_path, monkeypatch):
        """HandBrake could be uninstalled entirely and ripping would work."""
        from adr import encodertest

        monkeypatch.setattr(vaapi, "probe", lambda config: {
            "ok": True, "codecs": ["h264"], "detail": "ffmpeg encoded a clip."})
        ffmpeg = _script(tmp_path / "ffmpeg", 'for last; do :; done\nprintf v > "$last"\n')
        result = encodertest.test_encoder(self._config(tmp_path, ffmpeg_path=ffmpeg))
        assert result["ok"] is True, [s["detail"] for s in result["steps"]]

    def test_a_gpu_that_stops_working_is_reported(self, tmp_path, monkeypatch):
        from adr import encodertest

        monkeypatch.setattr(vaapi, "probe", lambda config: {
            "ok": False, "codecs": [], "detail": "ffmpeg could not encode on it."})
        result = encodertest.test_encoder(self._config(tmp_path))
        assert result["ok"] is False
        assert "could not encode" in result["summary"]

    def test_the_summary_names_the_encoder_that_was_tested(self, tmp_path, monkeypatch):
        """"HandBrake encoded a sample" after the GPU passed reads as a stale
        result from the previous configuration."""
        from adr import encodertest

        monkeypatch.setattr(vaapi, "probe", lambda config: {
            "ok": True, "codecs": ["h264"], "detail": "ok"})
        ffmpeg = _script(tmp_path / "ffmpeg", 'for last; do :; done\nprintf v > "$last"\n')
        result = encodertest.test_encoder(self._config(tmp_path, ffmpeg_path=ffmpeg))
        assert "HandBrake" not in result["summary"]
        assert "GPU" in result["summary"]

    def test_the_doctor_page_asks_the_same_question(self, tmp_path, monkeypatch):
        from adr import diagnostics

        monkeypatch.setattr(vaapi, "probe", lambda config: {
            "ok": True, "codecs": ["h264"], "detail": "ffmpeg encoded a clip."})
        check = diagnostics.check_hardware_encoding(self._config(tmp_path))
        assert check["status"] == "ok"
        assert "ffmpeg" in check["detail"]

    def test_the_missing_handbrake_preset_is_not_held_against_it(
        self, tmp_path, monkeypatch,
    ):
        from adr import diagnostics

        monkeypatch.setattr(vaapi, "probe", lambda config: {
            "ok": False, "codecs": [], "detail": "no GPU here."})
        check = diagnostics.check_hardware_encoding(self._config(tmp_path))
        assert check["status"] == "fail"
        assert "qsv" not in check["detail"], "the preset is not the subject"


class TestWhenTheInputIsNotAVideo:
    """The failure that cost an hour and read as an encoder fault.

    MakeMKV writes each title as it goes. A rip killed part-way — a service
    restart during an update, a cancel, a crash — leaves an MKV that looks
    perfectly ordinary in a directory listing and is truncated mid-frame.
    ffmpeg then says "Invalid data found when processing input", which sounds
    like the encoder and is nothing of the kind.
    """

    def _encode(self, tmp_path, stderr):
        exe = _script(tmp_path / "ffmpeg", f"""
            echo "{stderr}" >&2
            exit 183
        """)
        source = tmp_path / "B1_t00.mkv"
        source.write_bytes(b"M" * 5_000_000)
        encoder = vaapi.VaapiEncoder(_config(tmp_path, ffmpeg_path=exe))
        return encoder.encode(source, output_dir=tmp_path / "out")

    def test_it_names_the_file_rather_than_the_encoder(self, tmp_path):
        result = self._encode(
            tmp_path, "Error opening input files: Invalid data found when processing input")
        assert result.success is False
        assert "B1_t00.mkv" in result.error
        assert "not a readable video file" in result.error

    def test_it_says_what_actually_happened(self, tmp_path):
        result = self._encode(
            tmp_path, "Error opening input files: Invalid data found when processing input")
        assert "rip that was stopped part-way" in result.error
        assert "rip it again" in result.error, "it must say what to do"

    def test_it_keeps_ffmpegs_own_words(self, tmp_path):
        """Rewriting the cause is a claim; the original is the evidence."""
        result = self._encode(
            tmp_path, "Error opening input files: Invalid data found when processing input")
        assert "Invalid data found" in result.error

    def test_a_real_encoder_failure_is_not_blamed_on_the_input(self, tmp_path):
        """The rewrite has to be narrow, or a genuine encoder problem gets
        misdiagnosed as a bad file and someone re-rips for nothing."""
        result = self._encode(tmp_path, "Error while opening encoder - maybe incorrect parameters")
        assert "not a readable video file" not in result.error
        assert "incorrect parameters" in result.error
