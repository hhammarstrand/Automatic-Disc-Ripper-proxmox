"""Tests for the settings API.

The settings form is the only way most people will ever change a value, so a
setting the form can render but the API rejects is invisible until someone
presses Save and gets an error. The first test here is the one that matters:
it fails the moment a new default is added without being made settable.
"""

import pathlib
import re

import pytest

from adr.config import _DEFAULTS, Config
from adr.models import init_db
from web.app import create_app

#: Series mode has its own endpoints — it is a mode you switch on with state
#: that advances by itself, not a form field — so it is deliberately not part
#: of the settings form.
SERIES_MODE_KEYS = {k for k in _DEFAULTS if k.startswith("series_mode")}


def _allowed_keys() -> set[str]:
    """The key allowlist, read from the source rather than re-declared here."""
    source = pathlib.Path("web/app.py").read_text(encoding="utf-8")
    block = re.search(r"_ALLOWED_SETTINGS_KEYS = frozenset\(\{(.*?)\}\)", source, re.S)
    assert block, "the settings allowlist has moved or been renamed"
    return set(re.findall(r'"([a-z0-9_]+)"', block.group(1)))


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(
        f"raw_path: {tmp_path / 'raw'}\n"
        f"completed_path: {tmp_path / 'completed'}\n"
        f"staging_path: {tmp_path / 'staging'}\n",
    )
    init_db()
    app = create_app(Config(str(path)), pipeline_manager=None)
    app.config["TESTING"] = True
    return app.test_client()


class TestAllowlist:
    def test_every_setting_can_actually_be_saved(self):
        """A default the form shows but the API refuses is a Save button that
        fails for a reason nobody can see."""
        missing = sorted(set(_DEFAULTS) - _allowed_keys() - SERIES_MODE_KEYS)
        assert not missing, (
            f"settings the API will reject: {missing}. Add them to "
            "_ALLOWED_SETTINGS_KEYS in web/app.py."
        )

    def test_nothing_is_settable_that_is_not_a_setting(self):
        extra = sorted(_allowed_keys() - set(_DEFAULTS))
        assert not extra, f"allowlisted but not a real setting: {extra}"

    def test_an_unknown_key_is_refused(self, client):
        response = client.post("/api/settings", json={"rm_rf": "/"})
        assert response.status_code == 400
        assert "rm_rf" in response.get_json()["error"]


class TestDiscTypeSettings:
    def test_the_new_settings_save(self, client, tmp_path):
        response = client.post("/api/settings", json={
            "audio_cd_enabled": False,
            "audio_cd_format": "mp3",
            "audio_cd_mp3_bitrate": "192k",
            "music_path": str(tmp_path / "music"),
            "data_disc_enabled": False,
            "data_disc_path": str(tmp_path / "iso"),
        })
        assert response.status_code == 200
        config = response.get_json()["config"]
        assert config["audio_cd_format"] == "mp3"
        assert config["audio_cd_enabled"] is False
        assert config["data_disc_path"] == str(tmp_path / "iso")

    def test_an_unsupported_format_is_refused(self, client):
        response = client.post("/api/settings", json={"audio_cd_format": "ogg"})
        assert response.status_code == 400
        assert "flac" in response.get_json()["error"]

    def test_the_page_renders_with_both_sections(self, client):
        html = client.get("/settings").data.decode()
        assert "Audio CDs" in html
        assert "Data discs" in html

    def test_the_computed_folders_are_shown_as_placeholders(self, client, tmp_path):
        """music_path is stored empty and derived from completed_path, so the
        form has to say where albums go today or the field reads as unset."""
        html = client.get("/settings").data.decode()
        assert str(tmp_path / "completed" / "Music") in html
        assert str(tmp_path / "completed" / "ISO") in html


class TestDoctor:
    def test_the_audio_tool_check_is_on_the_page(self, client):
        ids = [check["id"] for check in client.get("/api/doctor").get_json()["checks"]]
        assert "audio_tools" in ids


class TestSwitchingHandBrakeOntoTheGPU:
    """The option the user actually wanted: keep HandBrake's preset tuning
    and use the hardware, rather than choosing between them."""

    def test_an_existing_encoder_flag_is_replaced_not_appended(self):
        """Two -e flags leave HandBrake with whichever it parses last, so
        changing encoders would appear to work once and then quietly stop."""
        from web.app import _without_encoder_flag

        assert _without_encoder_flag("-e qsv_h264 --verbose") == "--verbose"
        assert _without_encoder_flag("--encoder qsv_h265") == ""
        assert _without_encoder_flag("--encoder=qsv_h265 -x") == "-x"

    def test_unrelated_arguments_survive(self):
        from web.app import _without_encoder_flag

        assert _without_encoder_flag("--verbose --no-dvdnav") == "--verbose --no-dvdnav"

    def test_nothing_at_all(self):
        from web.app import _without_encoder_flag

        assert _without_encoder_flag("") == ""


class TestAnotherSiteCannotDriveTheApi:
    """The dashboard is unauthenticated by design — it sits on a home LAN —
    and that is a decision about who may reach it, not about who may drive it.
    A browser sends LAN requests whatever page asked, so any site the owner
    opens could POST to /api/jobs/1/delete or /api/update/start; the endpoints
    that read no JSON body need no CORS permission at all.
    """

    def _client(self, tmp_path):
        from adr.config import Config
        from adr.models import init_db
        from web.app import create_app

        path = tmp_path / "adr.yaml"
        path.write_text(
            f"raw_path: {tmp_path / 'raw'}\n"
            f"completed_path: {tmp_path / 'completed'}\n"
            f"staging_path: {tmp_path / 'staging'}\n",
        )
        init_db()
        app = create_app(Config(str(path)), pipeline_manager=None)
        app.config["TESTING"] = True
        return app.test_client()

    def test_a_post_from_another_origin_is_refused(self, tmp_path):
        response = self._client(tmp_path).post(
            "/api/jobs/delete", json={"ids": [1]},
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403

    def test_a_form_post_with_only_a_referer_is_refused(self, tmp_path):
        """The shape that needs no CORS permission: a plain form submission."""
        response = self._client(tmp_path).post(
            "/api/update/start",
            headers={"Referer": "https://evil.example/page.html"},
        )
        assert response.status_code == 403

    def test_the_dashboard_itself_is_allowed(self, tmp_path):
        client = self._client(tmp_path)
        response = client.post(
            "/api/jobs/delete", json={"ids": []},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code != 403

    def test_curl_is_still_welcome(self, tmp_path):
        """No Origin and no Referer is a deliberate client, not a browser
        being steered. This is somebody's homelab and scripting it is a
        legitimate thing to do, so the check is on a header that contradicts
        us rather than one that is absent."""
        response = self._client(tmp_path).post("/api/jobs/delete", json={"ids": []})
        assert response.status_code != 403

    def test_reading_is_never_refused(self, tmp_path):
        response = self._client(tmp_path).get(
            "/api/status", headers={"Origin": "https://evil.example"})
        assert response.status_code != 403
