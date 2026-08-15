"""The dashboard is opened from a home screen, not from a bookmark bar.

The phone asks for four things nobody ever told it to ask for — a favicon, two
spellings of a touch icon, and a manifest — and until this pass the answer to
all of them was 404. That is not only an ugly service log: without the manifest
and the icon, adding the page to the home screen produces a Safari chrome
window with a screenshot of the page as its tile, which is a bookmark, not the
appliance's own application.

None of this makes the app work offline and none of it is meant to. There is
deliberately no service worker: a ripper you cannot reach is a ripper that is
not running, and a cache that serves yesterday's dashboard would say the
opposite.
"""

import json
from pathlib import Path

import pytest

from adr.config import Config
from web.app import create_app

ICONS = Path("web/static/icons")
MANIFEST = Path("web/static/manifest.json")

#: The first eight bytes of every PNG ever written.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def client(tmp_path):
    from adr.models import init_db

    root = tmp_path / "app"
    root.mkdir()
    for name in ("raw", "completed", "staging"):
        (root / name).mkdir()
    config = Config(str(root / "adr.yaml"))
    config.update({
        "completed_path": str(root / "completed"),
        "raw_path": str(root / "raw"),
        "staging_path": str(root / "staging"),
    })
    init_db()
    return create_app(config).test_client()


class TestTheFilesTheBrowserAsksForItself:
    """Every one of these was a 404 in the service log, several per page load."""

    @pytest.mark.parametrize("path", [
        "/favicon.ico",
        "/apple-touch-icon.png",
        "/apple-touch-icon-precomposed.png",
        "/static/manifest.json",
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
    ])
    def test_it_is_actually_served(self, client, path):
        assert client.get(path).status_code == 200

    @pytest.mark.parametrize("path", [
        "/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
    ])
    def test_what_comes_back_is_a_png(self, client, path):
        assert client.get(path).get_data()[:8] == PNG_MAGIC


class TestTheIcons:
    NAMES = ["icon-32.png", "icon-192.png", "icon-512.png", "apple-touch-icon.png"]

    @pytest.mark.parametrize("name", NAMES)
    def test_the_file_is_a_png(self, name):
        assert (ICONS / name).read_bytes()[:8] == PNG_MAGIC

    @pytest.mark.parametrize("name,size", [
        ("icon-32.png", 32), ("icon-192.png", 192),
        ("icon-512.png", 512), ("apple-touch-icon.png", 180),
    ])
    def test_it_is_the_size_it_claims(self, name, size):
        """The width and height live in the IHDR, which is the 16 bytes after
        the signature and the chunk header. A manifest that promises 512 and
        ships 192 gets the icon rejected outright by Chrome."""
        import struct

        raw = (ICONS / name).read_bytes()
        width, height = struct.unpack(">II", raw[16:24])
        assert (width, height) == (size, size)

    def test_the_touch_icon_is_opaque(self):
        """iOS composites black behind transparency instead of leaving it
        clear, so a transparent corner is a black corner — and this icon is
        drawn on the page's own near-black, which would then be invisibly
        wrong rather than visibly wrong."""
        import struct
        import zlib

        raw = (ICONS / "apple-touch-icon.png").read_bytes()
        width, height, depth, colour = struct.unpack(">IIBB", raw[16:26])
        assert (depth, colour) == (8, 6), "not 8-bit RGBA"
        data, position = b"", 8
        while position < len(raw):
            (length,) = struct.unpack(">I", raw[position:position + 4])
            if raw[position + 4:position + 8] == b"IDAT":
                data += raw[position + 8:position + 8 + length]
            position += 12 + length
        flat = zlib.decompress(data)
        stride = width * 4
        for y in range(height):
            row = flat[y * (stride + 1) + 1:y * (stride + 1) + 1 + stride]
            assert set(row[3::4]) == {255}, f"row {y} is not fully opaque"

    def test_the_generator_needs_nothing_installed(self):
        """Pillow is not a dependency of this application and must not become
        one for the sake of four files that are regenerated about never."""
        source = Path("tools/make_icons.py").read_text()
        assert "from PIL" not in source and "import PIL" not in source
        assert "import zlib" in source and "import struct" in source


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST.read_text())


class TestTheManifest:
    def test_it_is_valid_json(self, manifest):
        assert manifest["name"] == "Automatic Disc Ripper"
        assert manifest["short_name"] == "ADR"

    def test_it_opens_as_an_application(self, manifest):
        """Anything other than standalone is a browser window with a bookmark
        in it, which is what this replaces."""
        assert manifest["display"] == "standalone"

    def test_it_is_the_dark_theme_from_the_first_frame(self, manifest):
        """The splash and the status bar are painted before any CSS loads. Left
        at the default they are white, which on this application is a flash of
        the one colour it never uses."""
        assert manifest["background_color"] == "#0d1117"
        assert manifest["theme_color"] == "#0d1117"

    def test_every_icon_it_promises_exists(self, manifest):
        for icon in manifest["icons"]:
            path = Path("web") / icon["src"].lstrip("/")
            assert path.exists(), f"the manifest promises {icon['src']}"
            assert path.read_bytes()[:8] == PNG_MAGIC

    def test_the_head_asks_for_all_of_it(self):
        head = Path("web/templates/base.html").read_text()
        assert 'name="theme-color"' in head
        assert 'rel="manifest"' in head
        assert 'rel="apple-touch-icon"' in head

    def test_there_is_no_service_worker(self):
        """Offline is meaningless for a machine that rips discs in another
        room, and a cache that mis-serves the dashboard is a real risk taken
        for none of the benefit."""
        assert not list(Path("web/static").glob("*service-worker*"))
        assert "serviceWorker" not in Path("web/static/js/app.js").read_text()
