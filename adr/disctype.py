"""What kind of disc is in the drive: video, audio CD, or data.

Until now every disc was handed to MakeMKV. That is right for a DVD or a
Blu-ray and wrong for the other two things people put in an optical drive: an
audio CD makes MakeMKV fail with an error about no titles, and a data disc
does the same. Both then look identical to a broken drive, which is the worst
possible failure mode — it sends you off debugging hardware that is fine.

Classification uses two sources, in this order:

1. **The table of contents**, read straight from the drive with the CDROM
   ioctls. Every track carries a control field whose bit 2 means "data". A
   disc with audio tracks and no data track is an audio CD; this is the one
   judgement here that is a fact rather than a guess.

2. **The ISO 9660 root directory**, read directly off the block device — no
   mounting, so it works as an unprivileged service. A ``VIDEO_TS`` directory
   means DVD-Video, ``BDMV`` means Blu-ray, ``AUDIO_TS`` means DVD-Audio.

Anything we cannot read is reported as video. That is deliberate: it is what
the application did before this module existed, so an unreadable-but-fine
Blu-ray keeps working exactly as it used to. A disc is only called *data* when
its root directory was read successfully and positively contained no video
structure — never merely because a probe failed.

Pure UDF Blu-rays have no ISO 9660 descriptor at all, so step 2 finds nothing
and the fallback returns video, which is the correct answer for them.
"""

from __future__ import annotations

import array
import contextlib
import errno
import fcntl
import logging
import os
import struct
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Kinds
# ------------------------------------------------------------------ #

KIND_VIDEO = "video"
KIND_AUDIO = "audio_cd"
KIND_DATA = "data"
KIND_EMPTY = "empty"

#: Every kind this module can return, for validation and UI listings.
ALL_KINDS = (KIND_VIDEO, KIND_AUDIO, KIND_DATA, KIND_EMPTY)


# ------------------------------------------------------------------ #
# CDROM ioctls
# ------------------------------------------------------------------ #

CDROMREADTOCHDR = 0x5305
CDROMREADTOCENTRY = 0x5306

CDROM_LBA = 0x01
CDROM_LEADOUT = 0xAA

# cdte_ctrl bit 2 marks a data track. Without it the track carries audio.
_CTRL_DATA = 0x04

# struct cdrom_tocentry: track, adr:4|ctrl:4, format, pad, lba (int), datamode, pad
_TOCENTRY = "<BBBxiB3x"

# The CD frame rate. Track offsets in a MusicBrainz disc ID are expressed in
# frames from the very start of the disc, and an LBA counts from the start of
# the first track — 2 seconds, or 150 frames, later.
FRAME_OFFSET = 150


@dataclass
class TocTrack:
    """One track from the disc's table of contents."""

    number: int
    lba: int
    is_audio: bool

    @property
    def frame_offset(self) -> int:
        """Absolute position in CD frames, as disc ID algorithms expect."""
        return self.lba + FRAME_OFFSET


@dataclass
class Toc:
    """A disc's table of contents."""

    first: int
    last: int
    leadout_lba: int
    tracks: list[TocTrack] = field(default_factory=list)

    @property
    def leadout_frame_offset(self) -> int:
        return self.leadout_lba + FRAME_OFFSET

    @property
    def audio_tracks(self) -> list[TocTrack]:
        return [t for t in self.tracks if t.is_audio]

    @property
    def data_tracks(self) -> list[TocTrack]:
        return [t for t in self.tracks if not t.is_audio]

    def duration_seconds(self, track: TocTrack) -> float:
        """Playing time of *track*, from where the next one starts.

        The last track runs to the lead-out. A CD plays 75 frames a second.
        """
        later = [t.lba for t in self.tracks if t.lba > track.lba]
        end = min(later) if later else self.leadout_lba
        return max(0.0, (end - track.lba) / 75.0)


def read_toc(device: str) -> Toc | None:
    """Read the table of contents from *device*.

    Returns None when the drive has no disc, refuses the ioctl, or the device
    cannot be opened — every one of which is a legitimate everyday state, so
    none of them is logged as an error.
    """
    fd = None
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        return _read_toc_fd(fd)
    except OSError as exc:
        if exc.errno not in (errno.ENOMEDIUM, errno.ENXIO, errno.EIO, errno.EINVAL):
            logger.debug("Could not read TOC from %s", device, exc_info=True)
        return None
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _read_toc_fd(fd: int) -> Toc | None:
    """Read a TOC from an already-open file descriptor."""
    hdr = array.array("B", [0, 0])
    fcntl.ioctl(fd, CDROMREADTOCHDR, hdr, True)
    first, last = int(hdr[0]), int(hdr[1])
    if first < 1 or last < first or last > 99:
        return None

    tracks: list[TocTrack] = []
    for number in range(first, last + 1):
        entry = _read_toc_entry(fd, number)
        if entry is None:
            return None
        lba, ctrl = entry
        tracks.append(TocTrack(number=number, lba=lba, is_audio=not (ctrl & _CTRL_DATA)))

    leadout = _read_toc_entry(fd, CDROM_LEADOUT)
    if leadout is None:
        return None

    return Toc(first=first, last=last, leadout_lba=leadout[0], tracks=tracks)


def _read_toc_entry(fd: int, number: int) -> tuple[int, int] | None:
    """Return ``(lba, ctrl)`` for one TOC entry, or None if the ioctl failed."""
    buf = array.array("B", struct.pack(_TOCENTRY, number, 0, CDROM_LBA, 0, 0))
    try:
        fcntl.ioctl(fd, CDROMREADTOCENTRY, buf, True)
    except OSError:
        logger.debug("CDROMREADTOCENTRY failed for track %s", number, exc_info=True)
        return None
    _track, adr_ctrl, _fmt, lba, _mode = struct.unpack(_TOCENTRY, buf.tobytes())
    # Little-endian bitfields fill from the least significant bit, so the
    # low nibble is adr and the high nibble is ctrl.
    return lba, (adr_ctrl >> 4) & 0x0F


# ------------------------------------------------------------------ #
# ISO 9660 root directory
# ------------------------------------------------------------------ #

SECTOR_SIZE = 2048
PVD_SECTOR = 16

#: Directories that identify a disc as video rather than data.
VIDEO_MARKERS = frozenset({"VIDEO_TS", "BDMV", "HVDVD_TS", "AVCHD"})

#: DVD-Audio. Rare, and MakeMKV cannot do anything with it, but naming it is
#: better than reporting a disc full of unreadable audio objects as "data".
AUDIO_DVD_MARKERS = frozenset({"AUDIO_TS"})

# A root directory that will not fit in this many bytes is not a disc we can
# make sense of; the cap stops a corrupt length field from reading a gigabyte.
_MAX_ROOT_BYTES = 4 * 1024 * 1024


def read_iso9660_root(path: str) -> list[str] | None:
    """Return the top-level names on an ISO 9660 volume, or None.

    *path* is a block device or an image file. None means "this is not an
    ISO 9660 volume, or it could not be read" — the caller must not read that
    as "the disc is empty".

    Reading is done with pread at sector-aligned offsets, so it needs nothing
    beyond read permission on the device. Nothing is mounted.
    """
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY)
        pvd = os.pread(fd, SECTOR_SIZE, PVD_SECTOR * SECTOR_SIZE)
        if len(pvd) < SECTOR_SIZE or pvd[0] != 1 or pvd[1:6] != b"CD001":
            return None

        # The root directory record is a fixed 34 bytes at offset 156 of the
        # primary volume descriptor.
        root = pvd[156:190]
        extent = int.from_bytes(root[2:6], "little")
        length = int.from_bytes(root[10:14], "little")
        if extent <= 0 or length <= 0 or length > _MAX_ROOT_BYTES:
            return None

        # Round up to whole sectors: directory records never straddle one.
        to_read = ((length + SECTOR_SIZE - 1) // SECTOR_SIZE) * SECTOR_SIZE
        data = os.pread(fd, to_read, extent * SECTOR_SIZE)
        if len(data) < length:
            return None
        return _parse_directory(data[:length])
    except OSError:
        logger.debug("Could not read ISO 9660 root of %s", path, exc_info=True)
        return None
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _parse_directory(data: bytes) -> list[str]:
    """Walk ISO 9660 directory records and return the entry names."""
    names: list[str] = []
    pos = 0
    while pos < len(data):
        rec_len = data[pos]
        if rec_len == 0:
            # Padding to the end of the sector; the next record starts at the
            # following sector boundary.
            pos = ((pos // SECTOR_SIZE) + 1) * SECTOR_SIZE
            continue
        if rec_len < 33 or pos + rec_len > len(data):
            break
        name_len = data[pos + 32]
        raw = data[pos + 33 : pos + 33 + name_len]
        pos += rec_len
        if name_len == 1 and raw in (b"\x00", b"\x01"):
            continue                       # the '.' and '..' entries
        name = raw.decode("ascii", "replace")
        # ISO 9660 appends a file version, ';1', to file names.
        name = name.split(";", 1)[0].rstrip(".")
        if name:
            names.append(name)
    return names


# ------------------------------------------------------------------ #
# Classification
# ------------------------------------------------------------------ #


@dataclass
class DiscInfo:
    """What we concluded about the disc, and why."""

    kind: str
    detail: str
    toc: Toc | None = None
    root_entries: list[str] = field(default_factory=list)

    @property
    def is_audio_cd(self) -> bool:
        return self.kind == KIND_AUDIO

    @property
    def is_video(self) -> bool:
        return self.kind == KIND_VIDEO

    @property
    def is_data(self) -> bool:
        return self.kind == KIND_DATA

    @property
    def track_count(self) -> int:
        return len(self.toc.audio_tracks) if self.toc else 0


def classify(device: str) -> DiscInfo:
    """Decide what kind of disc is in *device*.

    Never raises: an undecidable disc comes back as video, which is what the
    pipeline has always assumed.
    """
    toc = read_toc(device)

    if toc is not None and toc.audio_tracks and not toc.data_tracks:
        n = len(toc.audio_tracks)
        return DiscInfo(
            kind=KIND_AUDIO,
            detail=f"Audio CD with {n} track{'s' if n != 1 else ''}.",
            toc=toc,
        )

    if toc is not None and toc.audio_tracks and toc.data_tracks:
        # A mixed-mode CD: audio tracks plus a data track holding software or
        # extras. The audio is the part worth having, and it is what ARM rips.
        n = len(toc.audio_tracks)
        return DiscInfo(
            kind=KIND_AUDIO,
            detail=(
                f"Mixed-mode CD: {n} audio track{'s' if n != 1 else ''} plus a "
                "data track. The audio tracks will be ripped."
            ),
            toc=toc,
        )

    entries = read_iso9660_root(device)
    if entries is None:
        return DiscInfo(
            kind=KIND_VIDEO,
            detail="No ISO 9660 directory could be read; treating the disc as video.",
            toc=toc,
        )

    upper = {name.upper() for name in entries}
    found = sorted(upper & VIDEO_MARKERS)
    if found:
        return DiscInfo(
            kind=KIND_VIDEO,
            detail=f"Video disc: found {', '.join(found)}.",
            toc=toc,
            root_entries=entries,
        )
    if upper & AUDIO_DVD_MARKERS:
        return DiscInfo(
            kind=KIND_DATA,
            detail=(
                "DVD-Audio disc. There is no video to rip, so it is backed up "
                "as an image instead."
            ),
            toc=toc,
            root_entries=entries,
        )

    preview = ", ".join(sorted(entries)[:6]) or "nothing"
    return DiscInfo(
        kind=KIND_DATA,
        detail=f"Data disc: no video directory at the top level ({preview}).",
        toc=toc,
        root_entries=entries,
    )
