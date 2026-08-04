"""The logs page and the diagnostics bundle, over HTTP.

These are the two things that end a support conversation instead of starting
one: the service log without a shell, and everything else in one paste.
"""

import pytest

from adr import applog
from adr.config import Config
from adr.models import init_db
from web.app import create_app


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(
        f"raw_path: {tmp_path / 'raw'}\n"
        f"completed_path: {tmp_path / 'completed'}\n"
        f"staging_path: {tmp_path / 'staging'}\n"
        f"log_path: {tmp_path / 'logs'}\n"
        "tmdb_api_key: tmdb-secret-value\n",
    )
    init_db()
    return Config(str(path))


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setattr("adr.disc.diagnose_passthrough", lambda: {
        "drives": [], "problems": [], "ok": True,
    })
    app = create_app(config, pipeline_manager=None)
    app.config["TESTING"] = True
    return app.test_client()


def _log(config, lines):
    path = applog.log_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestTheLogsPage:
    def test_it_renders(self, client):
        response = client.get("/logs")
        assert response.status_code == 200
        assert "Service log" in response.data.decode()

    def test_it_is_in_the_navigation(self, client):
        assert 'href="/logs"' in client.get("/").data.decode()

    def test_the_api_returns_the_lines(self, client, config):
        _log(config, ["2026-08-04 07:26:01 [ERROR] adr.pipeline: the thing broke"])
        body = client.get("/api/logs").get_json()
        assert body["exists"] is True
        assert "the thing broke" in body["lines"][0]

    def test_the_api_filters_by_level(self, client, config):
        _log(config, [
            "2026-08-04 07:26:01 [INFO] adr.pipeline: ordinary",
            "2026-08-04 07:26:02 [ERROR] adr.pipeline: broken",
        ])
        lines = client.get("/api/logs?level=ERROR").get_json()["lines"]
        assert len(lines) == 1
        assert "broken" in lines[0]

    def test_the_api_searches(self, client, config):
        _log(config, [
            "2026-08-04 07:26:01 [INFO] adr.pipeline: ripping /dev/sr0",
            "2026-08-04 07:26:02 [INFO] adr.encoder: encoding",
        ])
        assert len(client.get("/api/logs?search=sr0").get_json()["lines"]) == 1

    def test_a_nonsense_line_count_does_not_500(self, client, config):
        _log(config, ["2026-08-04 07:26:01 [INFO] adr.pipeline: x"])
        assert client.get("/api/logs?lines=banana").status_code == 200
        assert client.get("/api/logs?lines=-5").status_code == 200
        assert client.get("/api/logs?lines=999999").status_code == 200

    def test_no_log_file_is_reported_not_an_error(self, client):
        body = client.get("/api/logs").get_json()
        assert body["exists"] is False
        assert body["lines"] == []


class TestTheBundleEndpoint:
    def test_it_is_plain_text_a_person_can_read(self, client):
        response = client.get("/api/diagnostics/bundle")
        assert response.status_code == 200
        assert response.mimetype == "text/plain"
        assert response.data.decode().startswith("Automatic Disc Ripper")

    def test_it_carries_no_secrets(self, client):
        """It exists to be pasted somewhere public."""
        text = client.get("/api/diagnostics/bundle").data.decode()
        assert "tmdb-secret-value" not in text
        assert "<set, redacted>" in text

    def test_it_includes_the_service_log(self, client, config):
        _log(config, ["2026-08-04 07:26:01 [ERROR] adr.encoder: HandBrake said no"])
        assert "HandBrake said no" in client.get("/api/diagnostics/bundle").data.decode()

    def test_the_doctor_page_points_at_it(self, client):
        assert "Copy diagnostics" in client.get("/doctor").data.decode()
