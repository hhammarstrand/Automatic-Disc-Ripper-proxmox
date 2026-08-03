"""Build ISO 9660 images by hand, so disc tests need no disc.

An image built here is a real ISO 9660 volume as far as the reader in
adr/disctype.py and adr/isobackup.py are concerned: a primary volume
descriptor at sector 16 pointing at a root directory of ordinary directory
records. That is the whole of the format those two modules parse.
"""

from __future__ import annotations

import struct

from adr.disctype import PVD_SECTOR, SECTOR_SIZE

#: Sector the root directory is written to. Arbitrary, but past the descriptors.
ROOT_EXTENT = 20


def dir_record(name: bytes, is_dir: bool = True) -> bytes:
    """One ISO 9660 directory record for *name*."""
    rec_len = 33 + len(name)
    if rec_len % 2:
        rec_len += 1                       # records are padded to even lengths
    rec = bytearray(rec_len)
    rec[0] = rec_len
    rec[25] = 0x02 if is_dir else 0x00
    rec[32] = len(name)
    rec[33 : 33 + len(name)] = name
    return bytes(rec)


def root_directory(names: list[bytes]) -> bytes:
    """A root directory holding '.', '..' and *names*."""
    data = bytearray()
    data += dir_record(b"\x00")            # .
    data += dir_record(b"\x01")            # ..
    for name in names:
        data += dir_record(name)
    return bytes(data)


def iso_image(
    names: list[bytes],
    root_len: int | None = None,
    volume_blocks: int | None = None,
    total_sectors: int | None = None,
) -> bytes:
    """A minimal ISO 9660 image whose root directory holds *names*.

    *volume_blocks* writes the volume space size field, which is what
    isobackup reads to learn how much of the disc is actually recorded.
    """
    root = root_directory(names)
    if root_len is None:
        root_len = len(root)
    if total_sectors is None:
        total_sectors = ROOT_EXTENT + 2
    if volume_blocks is None:
        volume_blocks = total_sectors

    pvd = bytearray(SECTOR_SIZE)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[80:84] = struct.pack("<I", volume_blocks)      # volume space size, LE
    pvd[84:88] = struct.pack(">I", volume_blocks)      # ...and BE
    pvd[128:130] = struct.pack("<H", SECTOR_SIZE)      # logical block size

    root_record = bytearray(34)
    root_record[0] = 34
    root_record[2:6] = struct.pack("<I", ROOT_EXTENT)
    root_record[10:14] = struct.pack("<I", root_len)
    root_record[25] = 0x02
    root_record[32] = 1
    pvd[156:190] = root_record

    image = bytearray(SECTOR_SIZE * total_sectors)
    image[PVD_SECTOR * SECTOR_SIZE : (PVD_SECTOR + 1) * SECTOR_SIZE] = pvd
    image[ROOT_EXTENT * SECTOR_SIZE : ROOT_EXTENT * SECTOR_SIZE + len(root)] = root
    return bytes(image)
