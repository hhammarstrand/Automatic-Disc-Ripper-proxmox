"""Tests for disc type classification.

The ISO 9660 reader is exercised against images built here byte by byte, so
the tests need no disc, no loop device and no root.
"""

import struct

import pytest

from adr import disctype
from adr.disctype import (
    KIND_AUDIO,
    KIND_DATA,
    KIND_VIDEO,
    SECTOR_SIZE,
    Toc,
    TocTrack,
)
from tests.isofixture import dir_record, iso_image


@pytest.fixture
def iso_path(tmp_path):
    def _make(names, root_len=None):
        path = tmp_path / "disc.iso"
        path.write_bytes(iso_image(names, root_len))
        return str(path)

    return _make


# ------------------------------------------------------------------ #
# read_iso9660_root
# ------------------------------------------------------------------ #

def test_reads_top_level_names(iso_path):
    entries = disctype.read_iso9660_root(iso_path([b"VIDEO_TS", b"JACKET_P"]))
    assert entries == ["VIDEO_TS", "JACKET_P"]


def test_dot_entries_are_not_names(iso_path):
    entries = disctype.read_iso9660_root(iso_path([b"BDMV"]))
    assert entries == ["BDMV"]


def test_file_version_suffix_is_stripped(iso_path):
    entries = disctype.read_iso9660_root(iso_path([b"README.TXT;1"]))
    assert entries == ["README.TXT"]


def test_non_iso_image_returns_none(tmp_path):
    path = tmp_path / "blank.bin"
    path.write_bytes(b"\x00" * (SECTOR_SIZE * 20))
    assert disctype.read_iso9660_root(str(path)) is None


def test_missing_file_returns_none(tmp_path):
    assert disctype.read_iso9660_root(str(tmp_path / "nope.iso")) is None


def test_absurd_root_length_is_refused(iso_path):
    # A corrupt length field must not make us read the whole disc into memory.
    assert disctype.read_iso9660_root(iso_path([b"VIDEO_TS"], root_len=1 << 30)) is None


def test_truncated_image_returns_none(tmp_path):
    path = tmp_path / "short.iso"
    path.write_bytes(iso_image([b"VIDEO_TS"])[: SECTOR_SIZE * 18])
    assert disctype.read_iso9660_root(str(path)) is None


def test_zero_record_skips_to_next_sector():
    # Two sectors of directory data with padding after the first entry.
    first = bytearray(SECTOR_SIZE)
    rec = dir_record(b"VIDEO_TS")
    first[: len(rec)] = rec
    second = bytearray(SECTOR_SIZE)
    rec2 = dir_record(b"BDMV")
    second[: len(rec2)] = rec2
    names = disctype._parse_directory(bytes(first + second))
    assert names == ["VIDEO_TS", "BDMV"]


# ------------------------------------------------------------------ #
# TOC
# ------------------------------------------------------------------ #

def _toc(*specs, leadout=200000):
    """Build a Toc from (lba, is_audio) pairs."""
    tracks = [
        TocTrack(number=i + 1, lba=lba, is_audio=is_audio)
        for i, (lba, is_audio) in enumerate(specs)
    ]
    return Toc(first=1, last=len(tracks), leadout_lba=leadout, tracks=tracks)


def test_frame_offset_adds_the_two_second_pregap():
    assert TocTrack(number=1, lba=0, is_audio=True).frame_offset == 150
    assert _toc((0, True), leadout=1000).leadout_frame_offset == 1150


def test_track_duration_runs_to_the_next_track():
    toc = _toc((0, True), (7500, True), leadout=15000)
    assert toc.duration_seconds(toc.tracks[0]) == pytest.approx(100.0)


def test_last_track_duration_runs_to_the_leadout():
    toc = _toc((0, True), (7500, True), leadout=15000)
    assert toc.duration_seconds(toc.tracks[1]) == pytest.approx(100.0)


def test_audio_and_data_tracks_are_separated():
    toc = _toc((0, True), (7500, False))
    assert [t.number for t in toc.audio_tracks] == [1]
    assert [t.number for t in toc.data_tracks] == [2]


def test_toc_entry_parses_ctrl_from_the_high_nibble():
    # ctrl=4 (data), adr=1 → the packed byte is 0x41.
    packed = struct.pack(disctype._TOCENTRY, 1, 0x41, disctype.CDROM_LBA, 12345, 0)
    _track, adr_ctrl, _fmt, lba, _mode = struct.unpack(disctype._TOCENTRY, packed)
    assert lba == 12345
    assert (adr_ctrl >> 4) & 0x0F == disctype._CTRL_DATA


def test_read_toc_on_a_plain_file_returns_none(tmp_path):
    path = tmp_path / "notadrive"
    path.write_bytes(b"x" * 10)
    assert disctype.read_toc(str(path)) is None


# ------------------------------------------------------------------ #
# classify
# ------------------------------------------------------------------ #

def test_all_audio_tracks_is_an_audio_cd(monkeypatch):
    monkeypatch.setattr(disctype, "read_toc", lambda d: _toc((0, True), (7500, True)))
    info = disctype.classify("/dev/sr0")
    assert info.kind == KIND_AUDIO
    assert info.track_count == 2
    assert "2 tracks" in info.detail


def test_single_audio_track_is_not_pluralised(monkeypatch):
    monkeypatch.setattr(disctype, "read_toc", lambda d: _toc((0, True)))
    assert "1 track." in disctype.classify("/dev/sr0").detail


def test_mixed_mode_cd_counts_as_audio(monkeypatch):
    monkeypatch.setattr(disctype, "read_toc", lambda d: _toc((0, True), (7500, False)))
    info = disctype.classify("/dev/sr0")
    assert info.kind == KIND_AUDIO
    assert "Mixed-mode" in info.detail


def test_video_ts_is_video(monkeypatch, iso_path):
    monkeypatch.setattr(disctype, "read_toc", lambda d: _toc((0, False)))
    info = disctype.classify(iso_path([b"VIDEO_TS"]))
    assert info.kind == KIND_VIDEO
    assert "VIDEO_TS" in info.detail


def test_bdmv_is_video(monkeypatch, iso_path):
    monkeypatch.setattr(disctype, "read_toc", lambda d: _toc((0, False)))
    assert disctype.classify(iso_path([b"BDMV", b"CERTIFICATE"])).kind == KIND_VIDEO


def test_data_disc_is_data(monkeypatch, iso_path):
    monkeypatch.setattr(disctype, "read_toc", lambda d: _toc((0, False)))
    info = disctype.classify(iso_path([b"SETUP.EXE", b"DOCS"]))
    assert info.kind == KIND_DATA
    assert "DOCS" in info.detail


def test_dvd_audio_is_backed_up_rather_than_ripped(monkeypatch, iso_path):
    monkeypatch.setattr(disctype, "read_toc", lambda d: _toc((0, False)))
    info = disctype.classify(iso_path([b"AUDIO_TS"]))
    assert info.kind == KIND_DATA
    assert "DVD-Audio" in info.detail


def test_unreadable_disc_falls_back_to_video(monkeypatch):
    """The behaviour before this module existed, kept for discs we cannot read.

    A pure-UDF Blu-ray has no ISO 9660 descriptor, so this is the path it
    takes — and video is the right answer for it.
    """
    monkeypatch.setattr(disctype, "read_toc", lambda d: None)
    monkeypatch.setattr(disctype, "read_iso9660_root", lambda d: None)
    info = disctype.classify("/dev/sr0")
    assert info.kind == KIND_VIDEO
    assert "treating the disc as video" in info.detail


def test_empty_root_directory_is_data(monkeypatch):
    monkeypatch.setattr(disctype, "read_toc", lambda d: None)
    monkeypatch.setattr(disctype, "read_iso9660_root", lambda d: [])
    info = disctype.classify("/dev/sr0")
    assert info.kind == KIND_DATA
    assert "nothing" in info.detail


def test_classify_of_a_device_that_is_not_there_is_video(tmp_path):
    """No disc, no drive, no exception — the caller gets the old behaviour."""
    info = disctype.classify(str(tmp_path / "sr9"))
    assert info.kind == KIND_VIDEO
