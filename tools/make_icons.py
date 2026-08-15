"""Draw the home-screen icons, with nothing installed to draw them.

    python tools/make_icons.py

Run once; the PNGs it writes are committed. Nobody has to run this to build or
deploy the application — it exists so the icons can be *regenerated* from a
description rather than being four binaries whose provenance is a comment.

Pillow is the obvious tool and is deliberately not used. This application ships
into an LXC container with a deliberately small dependency list, and an icon
generator that runs four times in the history of the project is a poor reason
to put an imaging library in requirements.txt — or, worse, to have icons that
can only be recreated on a machine that happens to have one. A PNG is a
signature, an IHDR, one zlib stream of filtered scanlines, and an IEND; zlib
and struct are in the standard library, so that is what this writes.

The glyph is a disc: the dark page colour as ground, the accent blue as the
data area, the near-white text colour as the hub — the same three colours the
theme uses, so the icon on the home screen and the page it opens are visibly
the same thing. Everything is rendered at four times the final size and
box-averaged down, which is the whole of the anti-aliasing story: no edge maths,
just enough samples that the average is right.

Fully opaque, all four. iOS composites its own black behind a transparent
apple-touch-icon and Android may do the same in a maskable slot, so a
transparent corner is not "no colour", it is "some colour chosen elsewhere".
"""

import struct
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ICONS = REPO / "web" / "static" / "icons"

#: The theme's own three colours (style.css: --adr-bg, --adr-accent, --adr-text).
GROUND = (0x0D, 0x11, 0x17)
ANNULUS = (0x58, 0xA6, 0xFF)
HUB = (0xE6, 0xED, 0xF3)

#: Radii as a fraction of the icon's width, from the centre outwards:
#: hub, the gap that separates hub from data area, and the data area itself.
HUB_RADIUS = 0.16
GAP_RADIUS = 0.22
DISC_RADIUS = 0.46

#: How many samples per axis go into one output pixel.
SUPERSAMPLE = 4


def _colour_at(dx: float, dy: float) -> tuple:
    """The colour of one sample, given its offset from the centre in widths."""
    r = (dx * dx + dy * dy) ** 0.5
    if r < HUB_RADIUS:
        return HUB
    if r < GAP_RADIUS:
        return GROUND
    if r < DISC_RADIUS:
        return ANNULUS
    return GROUND


def render(size: int) -> bytearray:
    """RGBA rows for a size×size icon, supersampled and box-averaged."""
    big = size * SUPERSAMPLE
    centre = big / 2.0
    samples = SUPERSAMPLE * SUPERSAMPLE

    # One row of the oversampled image at a time, accumulated into the output
    # row it belongs to. Keeping only SUPERSAMPLE rows alive matters at 512:
    # the oversampled image is 2048×2048, which is 16 MB held for no reason.
    out = bytearray()
    for y in range(size):
        acc = [[0, 0, 0] for _ in range(size)]
        for sy in range(SUPERSAMPLE):
            dy = ((y * SUPERSAMPLE + sy) + 0.5 - centre) / big
            for x in range(size):
                cell = acc[x]
                for sx in range(SUPERSAMPLE):
                    dx = ((x * SUPERSAMPLE + sx) + 0.5 - centre) / big
                    r, g, b = _colour_at(dx, dy)
                    cell[0] += r
                    cell[1] += g
                    cell[2] += b
        out.append(0)                      # filter byte: none, per scanline
        for cell in acc:
            out += bytes((cell[0] // samples, cell[1] // samples,
                          cell[2] // samples, 255))
    return out


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def write_png(path: Path, size: int, rows: bytes) -> None:
    """A PNG is a signature and three chunks. This writes those."""
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _chunk(b"IEND", b"")
    )


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    wanted = {
        "icon-32.png": 32,
        "icon-192.png": 192,
        "icon-512.png": 512,
        # 180 is what iOS asks for; anything else it rescales, badly.
        "apple-touch-icon.png": 180,
    }
    for name, size in wanted.items():
        write_png(ICONS / name, size, render(size))
        print(f"{name}: {size}×{size}, {(ICONS / name).stat().st_size} bytes")


if __name__ == "__main__":
    main()
