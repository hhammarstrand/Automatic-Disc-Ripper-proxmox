"""Tests for data-disc imaging.

create_image reads with pread and writes with a plain file handle, so an
ordinary file stands in for the block device perfectly — the code cannot tell
the difference, which is the point.
"""

import pytest

from adr import isobackup
from adr.disctype import SECTOR_SIZE
from tests.isofixture import iso_image


@pytest.fixture
def disc(tmp_path):
    """A synthetic 'disc' file. Returns (path, contents)."""
    def _make(total_sectors=24, volume_blocks=None, names=(b"SETUP.EXE",)):
        data = iso_image(
            list(names), volume_blocks=volume_blocks, total_sectors=total_sectors,
        )
        path = tmp_path / "sr0"
        path.write_bytes(data)
        return str(path), data

    return _make


# ------------------------------------------------------------------ #
# Size
# ------------------------------------------------------------------ #

def test_volume_size_comes_from_the_descriptor(disc):
    path, _ = disc(total_sectors=24, volume_blocks=22)
    assert isobackup.volume_size_bytes(path) == 22 * SECTOR_SIZE


def test_volume_size_of_a_non_iso_is_none(tmp_path):
    path = tmp_path / "blank"
    path.write_bytes(b"\x00" * (SECTOR_SIZE * 20))
    assert isobackup.volume_size_bytes(str(path)) is None


def test_volume_size_of_a_missing_file_is_none(tmp_path):
    assert isobackup.volume_size_bytes(str(tmp_path / "nope")) is None


def test_kernel_size_of_a_device_that_is_not_there_is_zero():
    assert isobackup.kernel_size_bytes("/dev/sr-does-not-exist") == 0


def test_image_size_prefers_the_descriptor(disc):
    """The drive routinely claims more than the disc holds; reading the extra
    produces I/O errors that look like failure and are not."""
    path, _ = disc(total_sectors=24, volume_blocks=22)
    assert isobackup.image_size(path) == 22 * SECTOR_SIZE


# ------------------------------------------------------------------ #
# Naming
# ------------------------------------------------------------------ #

def test_image_name_uses_the_label():
    assert isobackup.image_name("Windows 98") == "Windows 98.iso"


def test_image_name_without_a_label_falls_back():
    assert isobackup.image_name(None, "sr0") == "sr0.iso"
    assert isobackup.image_name("", "") == "Data disc.iso"


def test_image_name_sanitises():
    assert "/" not in isobackup.image_name("Disc 1/2")


def test_unique_path_does_not_overwrite(tmp_path):
    (tmp_path / "Disc.iso").write_bytes(b"first")
    assert isobackup.unique_path(tmp_path, "Disc.iso").name == "Disc (2).iso"


def test_unique_path_returns_the_plain_name_when_free(tmp_path):
    assert isobackup.unique_path(tmp_path, "Disc.iso").name == "Disc.iso"


# ------------------------------------------------------------------ #
# Imaging
# ------------------------------------------------------------------ #

def test_image_is_byte_for_byte(disc, tmp_path):
    path, data = disc(total_sectors=24, volume_blocks=24)
    out = tmp_path / "out"
    result = isobackup.create_image(path, out, label="Setup Disc")
    assert result.success
    assert result.path.name == "Setup Disc.iso"
    assert result.path.read_bytes() == data
    assert result.size_bytes == len(data)


def test_only_the_recorded_area_is_read(disc, tmp_path):
    """The file is 24 sectors; the descriptor says 22 are recorded."""
    path, data = disc(total_sectors=24, volume_blocks=22)
    result = isobackup.create_image(path, tmp_path / "out")
    assert result.size_bytes == 22 * SECTOR_SIZE
    assert result.path.read_bytes() == data[: 22 * SECTOR_SIZE]


def test_progress_reaches_one(disc, tmp_path):
    path, _ = disc(total_sectors=24, volume_blocks=24)
    seen = []
    isobackup.create_image(path, tmp_path / "out", progress_callback=seen.append)
    assert seen
    assert seen[-1]["overall"] == pytest.approx(1.0)
    assert seen[-1]["total_bytes"] == 24 * SECTOR_SIZE
    assert all(0.0 <= p["overall"] <= 1.0 for p in seen)


def test_a_size_we_cannot_work_out_is_reported_not_guessed(tmp_path):
    empty = tmp_path / "sr9"
    empty.write_bytes(b"")
    result = isobackup.create_image(str(empty), tmp_path / "out")
    assert not result.success
    assert "how large" in result.error


def test_cancelling_leaves_no_partial_image(disc, tmp_path):
    path, _ = disc(total_sectors=24, volume_blocks=24)
    out = tmp_path / "out"
    result = isobackup.create_image(path, out, should_cancel=lambda: True)
    assert not result.success
    assert result.error == "Cancelled."
    assert result.path is None
    assert list(out.iterdir()) == []


def test_an_unreadable_sector_deletes_the_image(disc, tmp_path, monkeypatch):
    """A half-copied ISO that looks complete is worse than no ISO at all."""
    path, _ = disc(total_sectors=24, volume_blocks=24)
    monkeypatch.setattr(isobackup, "_read_with_retries", lambda fd, size, offset: None)
    out = tmp_path / "out"
    result = isobackup.create_image(path, out)
    assert not result.success
    assert "could not be read" in result.error
    assert result.path is None
    assert list(out.iterdir()) == []


def test_a_short_disc_keeps_what_was_read(disc, tmp_path, monkeypatch):
    """The drive claimed more than the disc had; what came back is still good."""
    path, _ = disc(total_sectors=24, volume_blocks=24)
    real = isobackup._read_with_retries
    state = {"calls": 0}

    def stop_after_one(fd, size, offset):
        state["calls"] += 1
        return real(fd, size, offset) if state["calls"] == 1 else b""

    monkeypatch.setattr(isobackup, "_read_with_retries", stop_after_one)
    monkeypatch.setattr(isobackup, "CHUNK_SIZE", SECTOR_SIZE)
    result = isobackup.create_image(path, tmp_path / "out")
    assert result.success
    assert result.size_bytes == SECTOR_SIZE


def test_a_read_error_is_retried_before_giving_up(tmp_path, monkeypatch):
    attempts = {"n": 0}

    def flaky(fd, size, offset):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError("EIO")
        return b"ok"

    monkeypatch.setattr(isobackup, "_pread", flaky)
    monkeypatch.setattr(isobackup, "RETRY_DELAY", 0)
    assert isobackup._read_with_retries(0, 4, 0) == b"ok"
    assert attempts["n"] == 3


def test_retries_are_finite(monkeypatch):
    def always_fails(fd, size, offset):
        raise OSError("EIO")

    monkeypatch.setattr(isobackup, "_pread", always_fails)
    monkeypatch.setattr(isobackup, "RETRY_DELAY", 0)
    assert isobackup._read_with_retries(0, 4, 0) is None


def test_an_unwritable_destination_is_reported(disc, tmp_path):
    path, _ = disc(total_sectors=24, volume_blocks=24)
    blocker = tmp_path / "out"
    blocker.write_text("I am a file, not a directory")
    result = isobackup.create_image(path, blocker)
    assert not result.success
    assert "Could not prepare" in result.error


class TestACrashCannotLeaveACompleteLookingImage:
    """The image was written straight to its final name, so a process death
    mid-copy — an update, an OOM kill, a power cut, all routine per
    recovery.py — left a truncated ISO indistinguishable from a finished one,
    squatting the canonical name so the good re-image landed at "(2)"."""

    def test_the_writer_uses_a_part_name(self):
        import inspect

        from adr import isobackup

        source = inspect.getsource(isobackup.create_image)
        assert '".part"' in source
        assert "os.replace(part, target)" in source, (
            "the image is written to its final name again"
        )

    def test_stale_parts_are_swept(self, tmp_path):
        from adr import isobackup

        dead = tmp_path / "OLD_DISC.iso.part"
        dead.write_bytes(b"x" * 4096)
        finished = tmp_path / "GOOD_DISC.iso"
        finished.write_bytes(b"y" * 4096)

        removed = isobackup.sweep_stale_parts(tmp_path)
        assert removed == 1
        assert not dead.exists()
        assert finished.exists(), "a finished image was swept"

    def test_sweeping_a_missing_folder_is_nothing(self, tmp_path):
        from adr import isobackup

        assert isobackup.sweep_stale_parts(tmp_path / "nope") == 0


class TestTheDiscMustFitBeforeHoursOfReading:
    def test_too_small_a_destination_fails_immediately(self, tmp_path, monkeypatch):
        """An 8 GB disc into 5 GB free read for over an hour before ENOSPC
        discarded everything — when image_size() knew the answer up front."""
        import shutil as shutil_mod
        import types

        from adr import isobackup

        monkeypatch.setattr(isobackup, "image_size", lambda d: 8_000_000_000)
        monkeypatch.setattr(
            shutil_mod, "disk_usage",
            lambda p: types.SimpleNamespace(free=5_000_000_000, total=0, used=0),
        )
        result = isobackup.create_image(
            device="/dev/sr0", destination_dir=tmp_path, label="BIG",
        )
        assert not result.success
        assert "GB" in result.error and "free" in result.error
        assert list(tmp_path.iterdir()) == [], "something was written anyway"
