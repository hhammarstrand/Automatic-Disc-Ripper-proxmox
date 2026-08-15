"""Back up a data disc to an ISO image.

There is nothing to transcode on a data disc, so the only useful thing to do
with one is keep it — a byte-for-byte image that outlives the plastic.

The copy is done in Python rather than by shelling out to ``dd`` for two
reasons: progress, which dd only reports if you signal it, and the size
question. A drive routinely reports a larger capacity than the disc actually
holds, and reading past the recorded area produces I/O errors that look like a
failure but are not. The ISO 9660 primary volume descriptor states the real
volume size, so that is used when it is present, and the kernel's capacity
only as a fallback.

Read errors are retried before being treated as fatal. An optical drive
returning EIO once on a marginal sector and succeeding on the retry is
ordinary; giving up on the first one would throw away a recoverable disc.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from adr.disctype import PVD_SECTOR, SECTOR_SIZE
from adr.utils import sanitize_filename

logger = logging.getLogger(__name__)

#: Read size. A multiple of the 2048-byte sector, large enough that the
#: syscall overhead is irrelevant next to the drive's own latency.
CHUNK_SIZE = 64 * 1024

#: Headroom demanded beyond the image itself, so the copy cannot be the
#: thing that fills the disk to the byte and takes SQLite down with it.
SPACE_MARGIN = 512 * 1024 * 1024

#: How many times a failing read is retried before the backup gives up.
READ_RETRIES = 2

#: Pause between retries, to let a drive re-seek rather than hammering it.
RETRY_DELAY = 0.5

#: A seam for the tests. Patching ``os.pread`` itself would reach every other
#: user of it in the process, which is a wide blast radius for one assertion.
_pread = os.pread


@dataclass
class IsoResult:
    """What came of imaging one disc."""

    success: bool = False
    path: Path | None = None
    size_bytes: int = 0
    error: str | None = None


def volume_size_bytes(device: str) -> int | None:
    """The disc's recorded size from its ISO 9660 descriptor, or None.

    Field is at offset 80 of the primary volume descriptor: the number of
    logical blocks on the volume, little-endian. (Offset 84 holds the same
    number big-endian; ISO 9660 stores both.)
    """
    fd = None
    try:
        fd = os.open(device, os.O_RDONLY)
        pvd = os.pread(fd, SECTOR_SIZE, PVD_SECTOR * SECTOR_SIZE)
        if len(pvd) < SECTOR_SIZE or pvd[0] != 1 or pvd[1:6] != b"CD001":
            return None
        blocks = int.from_bytes(pvd[80:84], "little")
        block_size = int.from_bytes(pvd[128:130], "little") or SECTOR_SIZE
        size = blocks * block_size
        return size if size > 0 else None
    except OSError:
        logger.debug("Could not read volume size of %s", device, exc_info=True)
        return None
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def kernel_size_bytes(device: str) -> int:
    """The drive's reported capacity in bytes, from sysfs (512-byte units)."""
    name = Path(device).name
    try:
        sectors = int((Path("/sys/block") / name / "size").read_text().strip())
    except (OSError, ValueError):
        return 0
    return sectors * 512


def image_size(device: str) -> int:
    """How many bytes to read from *device*. Zero when we cannot tell."""
    return volume_size_bytes(device) or kernel_size_bytes(device)


def image_name(label: str | None, disc_type: str = "") -> str:
    """A filename for the image, from the volume label when there is one."""
    safe = sanitize_filename(label or "")
    if not safe:
        safe = sanitize_filename(disc_type) or "Data disc"
    return f"{safe}.iso"


def unique_path(directory: Path, filename: str) -> Path:
    """*directory/filename*, with a counter appended if it is taken.

    Imaging the same disc twice should not silently overwrite the first copy —
    the second attempt might be the one that failed halfway.
    """
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for n in range(2, 1000):
        candidate = directory / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
    raise OSError(f"Could not find a free filename for {filename} in {directory}")


def create_image(
    device: str,
    destination_dir: Path,
    label: str | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> IsoResult:
    """Copy *device* to an ISO image in *destination_dir*.

    Never raises for anything the disc or filesystem can do; the failure is
    reported in the result instead, with enough detail to act on.
    """
    result = IsoResult()

    total = image_size(device)
    if total <= 0:
        result.error = (
            f"Could not work out how large the disc in {device} is — the drive "
            "reports no capacity and the disc has no ISO 9660 descriptor. "
            "It may still be spinning up."
        )
        return result

    # Room for the whole image, asked before hours of reading rather than
    # discovered at ENOSPC after them. image_size() already knows the answer.
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(destination_dir).free
        if free < total + SPACE_MARGIN:
            result.error = (
                f"The disc needs {total / 1_073_741_824:.1f} GB and "
                f"{destination_dir} has {free / 1_073_741_824:.1f} GB free. "
                "Nothing was written — free some space or point the ISO "
                "folder somewhere larger under Settings."
            )
            return result
        target = unique_path(destination_dir, image_name(label, Path(device).name))
    except OSError as exc:
        result.error = f"Could not prepare {destination_dir}: {exc}"
        return result
    result.path = target

    # Written under a .part name, renamed only when complete. The image was
    # written straight to its final name once, and a process death mid-copy —
    # an update, an OOM kill, a power cut, all routine per recovery.py — left
    # a truncated ISO indistinguishable from a finished one, squatting the
    # canonical name so the good re-image landed at "(2)". _discard cannot
    # help there: nothing unwinds a killed process. The .part suffix is the
    # marker that survives death, and _sweep_stale_parts collects them.
    part = target.with_name(target.name + ".part")

    fd = None
    written = 0
    try:
        fd = os.open(device, os.O_RDONLY)
        with open(part, "wb") as out:
            while written < total:
                if should_cancel is not None and should_cancel():
                    result.error = "Cancelled."
                    _discard(part)
                    result.path = None
                    return result

                want = min(CHUNK_SIZE, total - written)
                chunk = _read_with_retries(fd, want, written)
                if chunk is None:
                    result.error = (
                        f"The disc could not be read at {written // SECTOR_SIZE} "
                        f"sectors in ({written / 1_048_576:.0f} MB of "
                        f"{total / 1_048_576:.0f} MB). This is the disc, not the "
                        "drive — a scratch or rot at that point."
                    )
                    _discard(part)
                    result.path = None
                    return result
                if not chunk:
                    # A short read at the end: the disc is smaller than the
                    # drive claimed. Everything read so far is still valid.
                    logger.info(
                        "%s ended early at %d of %d bytes; keeping what was read",
                        device, written, total,
                    )
                    break

                out.write(chunk)
                written += len(chunk)
                _report(progress_callback, written, total)

        result.size_bytes = written
        result.success = written > 0
        if not result.success:
            result.error = "Nothing could be read from the disc."
            _discard(part)
            result.path = None
            return result
        os.replace(part, target)
        return result
    except OSError as exc:
        result.error = f"Writing the image failed: {exc}"
        _discard(part)
        result.path = None
        return result
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _read_with_retries(fd: int, size: int, offset: int) -> bytes | None:
    """pread with retries. Returns the data, b"" at end of disc, None on failure."""
    for attempt in range(READ_RETRIES + 1):
        try:
            return _pread(fd, size, offset)
        except OSError as exc:
            logger.warning(
                "Read error at offset %d (attempt %d/%d): %s",
                offset, attempt + 1, READ_RETRIES + 1, exc,
            )
            if attempt < READ_RETRIES:
                time.sleep(RETRY_DELAY)
    return None


def _report(callback, written: int, total: int) -> None:
    if callback is None:
        return
    with contextlib.suppress(Exception):
        callback({
            "overall": min(1.0, written / total) if total else 0.0,
            "written_bytes": written,
            "total_bytes": total,
            "description": (
                f"Imaging disc — {written / 1_048_576:.0f} of "
                f"{total / 1_048_576:.0f} MB"
            ),
        })



def sweep_stale_parts(destination_dir) -> int:
    """Delete .iso.part files a dead process left behind. Returns how many.

    A part file is by definition incomplete: the writer renames it the moment
    the image is done, so one still wearing the suffix belonged to a process
    that died mid-copy. Nothing references it and nothing will finish it.
    """
    removed = 0
    directory = Path(str(destination_dir))
    if not directory.is_dir():
        return 0
    try:
        for stale in directory.glob("*.iso.part"):
            with contextlib.suppress(OSError):
                size = stale.stat().st_size
                stale.unlink()
                removed += 1
                logger.info(
                    "Removed %s — an image a previous run never finished "
                    "(%.1f GB reclaimed)", stale.name, size / 1_073_741_824,
                )
    except OSError:
        logger.debug("Could not sweep %s for stale parts", directory, exc_info=True)
    return removed


def _discard(path: Path) -> None:
    """Remove a half-written image. A partial ISO is worse than none."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
