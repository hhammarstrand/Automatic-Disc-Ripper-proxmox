"""Tests for audio CD ripping.

The two external tools are stood in for by shell scripts written into tmp_path.
That is slower than mocking subprocess, but it exercises the part that actually
breaks in production — argument handling, exit codes, output that arrives with
carriage returns rather than newlines.
"""

import os
import stat
import textwrap

import pytest

from adr import audiocd
from adr.audiocd import AudioCDRipper, album_folder, track_filename
from adr.disctype import Toc, TocTrack
from adr.musicbrainz import AlbumInfo, AlbumTrack

# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

class FakeConfig:
    """The handful of settings the ripper reads."""

    def __init__(self, tmp_path, **overrides):
        self.cdparanoia_path = overrides.get("cdparanoia_path", "/bin/true")
        self.ffmpeg_path = overrides.get("ffmpeg_path", "/bin/true")
        self.audio_cd_format = overrides.get("audio_cd_format", "flac")
        self.audio_cd_mp3_bitrate = overrides.get("audio_cd_mp3_bitrate", "320k")
        self.raw_path = tmp_path / "raw"


def _script(path, body):
    """Write an executable shell script and return its path as a string."""
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _toc(count=2):
    tracks = [
        TocTrack(number=i + 1, lba=i * 15000, is_audio=True) for i in range(count)
    ]
    return Toc(first=1, last=count, leadout_lba=count * 15000, tracks=tracks)


def _album(identified=True):
    if not identified:
        return AlbumInfo(disc_id="TESTDISCID_")
    return AlbumInfo(
        disc_id="TESTDISCID_",
        artist="Kent",
        album="Isola",
        year=1997,
        tracks=[AlbumTrack(number=1, title="Om du var här"), AlbumTrack(number=2, title="747")],
    )


# ------------------------------------------------------------------ #
# Naming
# ------------------------------------------------------------------ #

def test_album_folder_is_artist_then_album_and_year():
    assert str(album_folder(_album())) == "Kent/Isola (1997)"


def test_album_folder_without_a_year():
    assert str(album_folder(AlbumInfo(disc_id="x", artist="Kent", album="Isola"))) == "Kent/Isola"


def test_album_folder_without_an_artist():
    folder = album_folder(AlbumInfo(disc_id="x", album="Isola"))
    assert str(folder) == "Unknown Artist/Isola"


def test_unidentified_disc_is_filed_under_its_disc_id():
    """Stable, so re-ripping the same CD lands in the same folder rather than
    stacking up Unknown Album (2), (3), (4)."""
    folder = album_folder(AlbumInfo(disc_id="abc_def-"))
    assert str(folder) == "Unknown Artist/Unidentified CD abc_def-"


def test_track_filename_is_zero_padded():
    assert track_filename(3, "747", "flac") == "03 - 747.flac"


def test_track_filename_sanitises():
    assert "/" not in track_filename(1, "AC/DC", "mp3")


def test_track_filename_without_a_title():
    assert track_filename(7, "", "flac") == "07 - Track 07.flac"


# ------------------------------------------------------------------ #
# Tool checks
# ------------------------------------------------------------------ #

def test_missing_tools_names_what_is_missing(tmp_path):
    config = FakeConfig(tmp_path, cdparanoia_path="/nowhere/cdparanoia",
                        ffmpeg_path="/nowhere/ffmpeg")
    assert audiocd.missing_tools(config) == ["/nowhere/cdparanoia", "/nowhere/ffmpeg"]


def test_present_tools_are_not_reported(tmp_path):
    assert audiocd.missing_tools(FakeConfig(tmp_path)) == []


def test_rip_refuses_without_the_tools(tmp_path):
    config = FakeConfig(tmp_path, cdparanoia_path="/nowhere/cdparanoia")
    result = AudioCDRipper(config).rip("/dev/sr0", 1, _toc(), _album(), tmp_path / "out")
    assert not result.success
    assert "cdparanoia" in result.error
    assert "apt install" in result.error


def test_rip_refuses_an_unknown_format(tmp_path):
    config = FakeConfig(tmp_path, audio_cd_format="ogg")
    result = AudioCDRipper(config).rip("/dev/sr0", 1, _toc(), _album(), tmp_path / "out")
    assert not result.success
    assert "flac, mp3" in result.error


def test_rip_refuses_a_disc_with_no_audio_tracks(tmp_path):
    toc = Toc(first=1, last=1, leadout_lba=1000,
              tracks=[TocTrack(number=1, lba=0, is_audio=False)])
    result = AudioCDRipper(FakeConfig(tmp_path)).rip("/dev/sr0", 1, toc, _album(), tmp_path / "out")
    assert not result.success
    assert "no audio tracks" in result.error


# ------------------------------------------------------------------ #
# Ripping, with the tools stood in for
# ------------------------------------------------------------------ #

@pytest.fixture
def tools(tmp_path):
    """A cdparanoia that writes a WAV and an ffmpeg that copies it."""
    cdparanoia = _script(tmp_path / "cdparanoia", """
        # args: -d DEV -w TRACK OUTFILE
        out="$5"
        printf 'RIFFfake' > "$out"
        echo "(== PROGRESS == [    | 000100 00 ] == :^D * ==)"
    """)
    ffmpeg = _script(tmp_path / "ffmpeg", """
        # the output file is the last argument
        for last; do :; done
        printf 'encoded' > "$last"
    """)
    return FakeConfig(tmp_path, cdparanoia_path=cdparanoia, ffmpeg_path=ffmpeg)


def test_a_whole_cd_is_ripped_and_encoded(tools, tmp_path):
    out = tmp_path / "music"
    result = AudioCDRipper(tools).rip("/dev/sr0", 1, _toc(2), _album(), out)
    assert result.success
    assert result.failed_tracks == []
    names = sorted(p.name for p in result.files)
    assert names == ["01 - Om du var här.flac", "02 - 747.flac"]
    assert result.output_dir == out / "Kent" / "Isola (1997)"
    assert all(p.exists() for p in result.files)


def test_the_wav_scratch_files_are_cleaned_up(tools, tmp_path):
    AudioCDRipper(tools).rip("/dev/sr0", 7, _toc(2), _album(), tmp_path / "music")
    assert not (tools.raw_path / "7-audio").exists()


def test_progress_runs_from_zero_to_one(tools, tmp_path):
    seen = []
    AudioCDRipper(tools).rip("/dev/sr0", 1, _toc(2), _album(), tmp_path / "music",
                             progress_callback=seen.append)
    assert seen[0]["overall"] == 0.0
    assert seen[-1]["overall"] == 1.0
    assert seen[-1]["track_total"] == 2
    assert all(0.0 <= p["overall"] <= 1.0 for p in seen)


def test_a_failing_track_does_not_lose_the_others(tmp_path):
    """One scratch should cost one track, not the album."""
    cdparanoia = _script(tmp_path / "cdparanoia", """
        out="$5"
        if [ "$4" = "2" ]; then exit 1; fi
        printf 'RIFFfake' > "$out"
    """)
    ffmpeg = _script(tmp_path / "ffmpeg", """
        for last; do :; done
        printf 'encoded' > "$last"
    """)
    config = FakeConfig(tmp_path, cdparanoia_path=cdparanoia, ffmpeg_path=ffmpeg)
    result = AudioCDRipper(config).rip("/dev/sr0", 1, _toc(3), _album(), tmp_path / "music")
    assert result.success
    assert result.failed_tracks == [2]
    assert len(result.files) == 2
    assert "failed: 2" in result.error


def test_a_cd_where_every_track_fails_is_a_failure(tmp_path):
    cdparanoia = _script(tmp_path / "cdparanoia", "exit 1\n")
    config = FakeConfig(tmp_path, cdparanoia_path=cdparanoia)
    result = AudioCDRipper(config).rip("/dev/sr0", 1, _toc(2), _album(), tmp_path / "music")
    assert not result.success
    assert "the disc is damaged rather than that the drive is" in result.error


def test_an_empty_wav_counts_as_a_failure(tmp_path):
    """cdparanoia exiting 0 having written nothing must not become a 0-byte FLAC."""
    cdparanoia = _script(tmp_path / "cdparanoia", ': > "$5"\n')
    config = FakeConfig(tmp_path, cdparanoia_path=cdparanoia)
    result = AudioCDRipper(config).rip("/dev/sr0", 1, _toc(1), _album(), tmp_path / "music")
    assert not result.success


def test_a_failing_encoder_drops_the_track(tmp_path):
    cdparanoia = _script(tmp_path / "cdparanoia", 'printf x > "$5"\n')
    ffmpeg = _script(tmp_path / "ffmpeg", "exit 2\n")
    config = FakeConfig(tmp_path, cdparanoia_path=cdparanoia, ffmpeg_path=ffmpeg)
    result = AudioCDRipper(config).rip("/dev/sr0", 1, _toc(1), _album(), tmp_path / "music")
    assert not result.success
    assert result.failed_tracks == [1]


def test_tool_failures_reach_the_log_sink(tmp_path):
    cdparanoia = _script(tmp_path / "cdparanoia", 'echo "unable to read"; exit 1\n')
    config = FakeConfig(tmp_path, cdparanoia_path=cdparanoia)
    ripper = AudioCDRipper(config)
    lines = []
    ripper.log_sink = lines.append
    ripper.rip("/dev/sr0", 1, _toc(1), _album(), tmp_path / "music")
    assert any("unable to read" in line for line in lines)


def test_mp3_uses_the_configured_bitrate(tmp_path):
    """The arguments matter more than the output here, so they are recorded."""
    argfile = tmp_path / "args.txt"
    ffmpeg = _script(tmp_path / "ffmpeg", f"""
        echo "$@" >> {argfile}
        for last; do :; done
        printf 'encoded' > "$last"
    """)
    cdparanoia = _script(tmp_path / "cdparanoia", 'printf x > "$5"\n')
    config = FakeConfig(tmp_path, cdparanoia_path=cdparanoia, ffmpeg_path=ffmpeg,
                        audio_cd_format="mp3", audio_cd_mp3_bitrate="192k")
    result = AudioCDRipper(config).rip("/dev/sr0", 1, _toc(1), _album(), tmp_path / "music")
    assert result.success
    args = argfile.read_text()
    assert "libmp3lame" in args
    assert "-b:a 192k" in args
    assert result.files[0].suffix == ".mp3"


def test_tags_are_passed_to_the_encoder(tmp_path):
    argfile = tmp_path / "args.txt"
    ffmpeg = _script(tmp_path / "ffmpeg", f"""
        echo "$@" >> {argfile}
        for last; do :; done
        printf 'encoded' > "$last"
    """)
    cdparanoia = _script(tmp_path / "cdparanoia", 'printf x > "$5"\n')
    config = FakeConfig(tmp_path, cdparanoia_path=cdparanoia, ffmpeg_path=ffmpeg)
    AudioCDRipper(config).rip("/dev/sr0", 1, _toc(2), _album(), tmp_path / "music")
    args = argfile.read_text()
    assert "album=Isola" in args
    assert "artist=Kent" in args
    assert "date=1997" in args
    assert "track=1/2" in args


def test_an_unidentified_cd_still_rips(tools, tmp_path):
    result = AudioCDRipper(tools).rip("/dev/sr0", 1, _toc(2), _album(identified=False),
                                      tmp_path / "music")
    assert result.success
    assert sorted(p.name for p in result.files) == ["01 - Track 01.flac", "02 - Track 02.flac"]


# ------------------------------------------------------------------ #
# Process plumbing
# ------------------------------------------------------------------ #

def test_run_returns_the_exit_code_and_output(tmp_path):
    ripper = AudioCDRipper(FakeConfig(tmp_path))
    code, tail = ripper._run(["/bin/sh", "-c", "echo hello; exit 3"], 10, None)
    assert code == 3
    assert "hello" in tail


def test_run_splits_carriage_returns(tmp_path):
    """cdparanoia redraws its progress bar with \\r and never a newline."""
    ripper = AudioCDRipper(FakeConfig(tmp_path))
    lines = []
    ripper._run(["/bin/sh", "-c", "printf 'one\\rtwo\\rthree\\n'"], 10, lines.append)
    assert lines == ["one", "two", "three"]


def test_run_kills_a_tool_that_hangs(tmp_path):
    ripper = AudioCDRipper(FakeConfig(tmp_path))
    code, tail = ripper._run(["/bin/sh", "-c", "sleep 30"], 1, None)
    assert code == -1
    assert "Timed out" in tail


def test_run_reports_a_missing_binary(tmp_path):
    ripper = AudioCDRipper(FakeConfig(tmp_path))
    code, tail = ripper._run([str(tmp_path / "not-a-program")], 5, None)
    assert code == -1
    assert tail


def test_a_raising_progress_callback_does_not_break_the_rip(tools, tmp_path):
    def explode(_info):
        raise RuntimeError("dashboard is on fire")

    result = AudioCDRipper(tools).rip("/dev/sr0", 1, _toc(1), _album(), tmp_path / "music",
                                      progress_callback=explode)
    assert result.success


def test_progress_regex_reads_the_sector():
    match = audiocd._PROGRESS_RE.search("(== PROGRESS == [  >>  | 010304 00 ] == :^D * ==)")
    assert match and match.group(1) == "010304"


def test_output_directory_is_created(tools, tmp_path):
    target = tmp_path / "a" / "b" / "music"
    result = AudioCDRipper(tools).rip("/dev/sr0", 1, _toc(1), _album(), target)
    assert result.success
    assert (target / "Kent" / "Isola (1997)").is_dir()


def test_files_are_written_where_the_result_says(tools, tmp_path):
    result = AudioCDRipper(tools).rip("/dev/sr0", 1, _toc(1), _album(), tmp_path / "music")
    for path in result.files:
        assert path.parent == result.output_dir
        assert os.path.getsize(path) > 0


class TestCancelStopsBetweenTracks:
    """Killing the running cdparanoia is not enough on its own: that track is
    recorded as failed and the loop starts a fresh cdparanoia for the next —
    the registry has already handed out its kill, so a fifteen-track CD
    cancelled at track two ripped the other thirteen anyway."""

    def test_the_loop_asks_between_tracks(self, tmp_path):
        import types

        from adr.audiocd import AudioCDRipper
        from adr.disctype import Toc, TocTrack

        # The tool-presence check wants real executables; any file with the
        # right bits set will do, since _extract is stubbed below.
        for name in ("cdparanoia", "ffmpeg"):
            exe = tmp_path / name
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
        config = types.SimpleNamespace(
            raw_path=tmp_path / "raw",
            ffmpeg_path=str(tmp_path / "ffmpeg"),
            cdparanoia_path=str(tmp_path / "cdparanoia"),
            audio_cd_format="flac", audio_cd_mp3_bitrate="320k",
        )
        ripper = AudioCDRipper(config)
        extracted = []
        ripper._extract = lambda *a, **k: extracted.append(1) or True

        toc = Toc(first=1, last=3, leadout_lba=100000, tracks=[
            TocTrack(number=n, lba=n * 10000, is_audio=True) for n in (1, 2, 3)
        ])
        from adr.musicbrainz import AlbumInfo

        album = AlbumInfo(disc_id="x" * 28)
        calls = {"n": 0}

        def cancel_after_first():
            calls["n"] += 1
            return calls["n"] > 1          # first track runs, then cancel

        result = ripper.rip(
            "/dev/sr0", 1, toc, album, tmp_path / "music",
            should_cancel=cancel_after_first,
        )
        assert result.success is False
        assert result.error == "Cancelled."
        assert len(extracted) <= 1, "tracks kept ripping after the cancel"


class TestMetadataCannotEscapeTheMusicFolder:
    def test_dot_dot_as_artist_stays_inside(self):
        """sanitize_filename strips slashes but not dots, and the artist name
        arrives over the network from MusicBrainz."""
        from adr.audiocd import album_folder
        from adr.musicbrainz import AlbumInfo

        album = AlbumInfo(disc_id="y" * 28, artist="..", album="Album")
        album.tracks = []
        folder = album_folder(album)
        assert ".." not in folder.parts
        assert folder.parts[0] == "Unknown Artist"
