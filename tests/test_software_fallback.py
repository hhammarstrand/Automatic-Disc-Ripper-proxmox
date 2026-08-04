"""Escaping a hardware preset without touching the Proxmox host.

Passing a GPU through means editing the container's config, which the
container cannot do and should not be able to. Changing the preset needs
nothing but the web UI — so for someone without host access that is the fix,
and it should be a button rather than a sentence about where to go.

The property that matters: the switch is *proved* before it is kept. A preset
that cannot encode looks exactly like one that can until something tries.
"""

import stat
import textwrap
import types

import pytest

from adr import encodertest
from adr.config import Config
from adr.models import init_db
from web.app import create_app

PRESET_LIST = """
General/
  Fast 1080p30
    Fast 1080p30 description here.
  HQ 1080p30 Surround
    HQ description.
  Super HQ 1080p30 Surround
    Super HQ description.
Hardware/
  H.265 QSV 1080p
    Uses Quick Sync.
  H.264 NVENC 1080p
    Uses NVENC.
"""


def _script(path, body):
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def handbrake(tmp_path):
    """A HandBrake that lists presets and encodes only software ones."""
    listing = tmp_path / "presets.txt"
    listing.write_text(PRESET_LIST)
    return _script(tmp_path / "HandBrakeCLI", f"""
        wants_list=0
        for a in "$@"; do case "$a" in --preset-list) wants_list=1;; esac; done
        if [ $wants_list = 1 ]; then cat {listing}; exit 0; fi
        for a in "$@"; do
          case "$a" in
            --preset=*QSV*|--preset=*NVENC*)
              echo "ERROR: encqsvInit: qsv is not available on the system" >&2
              echo "Encode failed (error 3)." >&2
              exit 3;;
          esac
        done
        prev=""
        for a in "$@"; do
          if [ "$prev" = "-o" ]; then printf video > "$a"; fi
          prev="$a"
        done
    """)


@pytest.fixture
def ffmpeg(tmp_path):
    return _script(tmp_path / "ffmpeg", 'for last; do :; done; printf x > "$last"\n')


@pytest.fixture
def config(tmp_path, handbrake, ffmpeg):
    path = tmp_path / "adr.yaml"
    path.write_text(
        f"raw_path: {tmp_path / 'raw'}\n"
        f"completed_path: {tmp_path / 'completed'}\n"
        f"staging_path: {tmp_path / 'staging'}\n"
        f"handbrake_path: {handbrake}\n"
        f"ffmpeg_path: {ffmpeg}\n"
        "handbrake_preset: H.265 QSV 1080p\n"
        "handbrake_preset_file: ''\n",
    )
    init_db()
    return Config(str(path))


@pytest.fixture
def client(config):
    app = create_app(config, pipeline_manager=None)
    app.config["TESTING"] = True
    return app.test_client()


class TestChoosingAReplacement:
    def test_hardware_presets_are_not_offered(self, config):
        options = encodertest.software_alternatives(config)
        assert options
        assert not any("QSV" in o or "NVENC" in o for o in options)

    def test_the_closest_name_comes_first(self, tmp_path, handbrake, ffmpeg):
        """Someone who chose 'Super HQ 1080p30 Surround (Svenska)' wants that
        quality, not the first entry in an alphabetical list."""
        config = types.SimpleNamespace(
            handbrake_path=handbrake, ffmpeg_path=ffmpeg,
            handbrake_preset="Super HQ 1080p30 Surround (Svenska)",
            handbrake_preset_file="", handbrake_extra_args="",
        )
        assert encodertest.software_alternatives(config)[0] == "Super HQ 1080p30 Surround"

    def test_a_localised_copy_still_matches_its_original(self, tmp_path, handbrake, ffmpeg):
        config = types.SimpleNamespace(
            handbrake_path=handbrake, ffmpeg_path=ffmpeg,
            handbrake_preset="HQ 1080p30 Surround (Deutsch)",
            handbrake_preset_file="", handbrake_extra_args="",
        )
        assert "HQ 1080p30 Surround" in encodertest.software_alternatives(config)

    def test_a_handbrake_that_lists_nothing_offers_nothing(self, tmp_path, ffmpeg):
        silent = _script(tmp_path / "hb-silent", "exit 0\n")
        config = types.SimpleNamespace(
            handbrake_path=silent, ffmpeg_path=ffmpeg,
            handbrake_preset="Whatever", handbrake_preset_file="",
            handbrake_extra_args="",
        )
        assert encodertest.software_alternatives(config) == []


class TestSwitchingOver:
    def test_the_api_lists_the_options(self, client):
        body = client.get("/api/encoder/software-options").get_json()
        assert body["current"] == "H.265 QSV 1080p"
        assert "Fast 1080p30" in body["options"]

    def test_switching_proves_it_works_before_keeping_it(self, client, config):
        response = client.post("/api/encoder/use-software",
                               json={"preset": "Super HQ 1080p30 Surround"})
        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert body["test"]["ok"] is True, "it must have actually encoded"
        config.load()
        assert config.handbrake_preset == "Super HQ 1080p30 Surround"

    def test_with_no_preset_named_it_picks_the_best_one(self, client, config):
        assert client.post("/api/encoder/use-software", json={}).status_code == 200
        config.load()
        assert "QSV" not in config.handbrake_preset

    def test_a_preset_that_also_fails_changes_nothing(self, client, config, monkeypatch):
        """Leaving a preset in place that has just been shown not to work is a
        worse state than the one we started in."""
        monkeypatch.setattr(
            encodertest, "test_encoder",
            lambda cfg: {"ok": False, "summary": "still broken", "steps": []},
        )
        response = client.post("/api/encoder/use-software",
                               json={"preset": "Fast 1080p30"})
        assert response.status_code == 409
        assert response.get_json()["reverted_to"] == "H.265 QSV 1080p"
        config.load()
        assert config.handbrake_preset == "H.265 QSV 1080p"

    def test_an_arbitrary_preset_is_refused(self, client, config):
        """This exists to escape a hardware preset, not to set any string."""
        response = client.post("/api/encoder/use-software",
                               json={"preset": "H.265 QSV 1080p"})
        assert response.status_code == 400
        config.load()
        assert config.handbrake_preset == "H.265 QSV 1080p"

    def test_the_page_offers_the_button(self, client):
        html = client.get("/doctor").data.decode()
        assert "encoderRescue" in html
        assert "Encode in software instead" in html


def test_every_software_preset_is_offered_not_just_the_similar_ones(config):
    """Resemblance orders the list; it must not shorten it. Dropping the
    presets that happen not to resemble the broken name would hide the good
    ones — which was exactly the bug the first version of this had."""
    options = encodertest.software_alternatives(config)
    assert set(options) == {
        "Fast 1080p30", "HQ 1080p30 Surround", "Super HQ 1080p30 Surround",
    }
