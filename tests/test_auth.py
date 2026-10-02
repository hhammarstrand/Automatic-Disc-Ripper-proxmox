"""The optional password in front of the web UI."""

import pytest
import yaml

from adr import auth
from adr.config import Config
from web.app import create_app

@pytest.fixture
def config(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(yaml.safe_dump({
        "raw_path": str(tmp_path / "raw"),
        "completed_path": str(tmp_path / "completed"),
        "staging_path": str(tmp_path / "staging"),
        "notify_enabled": False,
    }))
    return Config(str(path))


@pytest.fixture
def client(config, monkeypatch):
    import web.app as app_mod

    from adr.models import init_db

    init_db()
    monkeypatch.setattr(app_mod.time, "sleep", lambda s: None)
    auth._failures.clear()
    app = create_app(config)
    app.config["TESTING"] = True
    return _lan(app.test_client())


def _lan(client, address="10.10.0.5"):
    client.environ_base["REMOTE_ADDR"] = address
    return client


def test_without_a_password_nothing_changes(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 200
    assert client.get("/login").status_code == 302, "nothing to sign in to"


class TestWithAPassword:
    @pytest.fixture(autouse=True)
    def password(self, config):
        auth.set_password(config, "hemligt-lösen")

    def test_pages_send_you_to_sign_in_and_back(self, client):
        response = client.get("/history?page=2")
        assert response.status_code == 302
        assert "/login?next=/history?page%3D2" in response.headers["Location"]

    def test_the_api_answers_401_not_a_login_page(self, client):
        response = client.get("/api/status")
        assert response.status_code == 401
        assert response.get_json()["login"] is True

    def test_static_files_stay_reachable(self, client):
        assert client.get("/static/manifest.json").status_code == 200

    def test_the_right_password_signs_in(self, client):
        response = client.post("/login", data={"password": "hemligt-lösen", "next": "/history"})
        assert response.status_code == 302 and response.headers["Location"].endswith("/history")
        assert client.get("/api/status").status_code == 200

    def test_a_wrong_password_does_not(self, client):
        response = client.post("/login", data={"password": "fel"})
        assert response.status_code == 401
        assert client.get("/api/status").status_code == 401

    def test_guessing_is_stopped_after_five(self, client):
        for _ in range(5):
            client.post("/login", data={"password": "fel"})
        response = client.post("/login", data={"password": "hemligt-lösen"})
        assert b"Too many" in response.data
        assert client.get("/api/status").status_code == 401

    def test_sign_in_never_redirects_off_site(self, client):
        response = client.post("/login", data={"password": "hemligt-lösen",
                                               "next": "//evil.example/phish"})
        assert response.headers["Location"].endswith("/")
        assert "evil" not in response.headers["Location"]

    def test_changing_the_password_signs_every_browser_out(self, client, config):
        client.post("/login", data={"password": "hemligt-lösen"})
        other = _lan(client.application.test_client())
        other.post("/login", data={"password": "hemligt-lösen"})

        assert client.post("/api/auth/password", json={"password": "nytt-lösen"}).status_code == 200

        assert client.get("/api/status").status_code == 200, "the one who changed it stays in"
        assert other.get("/api/status").status_code == 401

    def test_the_container_itself_is_let_through(self, client):
        """update.sh checks /api/status on 127.0.0.1 after every update."""
        local = _lan(client.application.test_client(), "127.0.0.1")
        assert local.get("/api/status").status_code == 200

    def test_only_a_hash_is_stored(self, config):
        stored = config.as_dict()[auth.HASH_KEY]
        assert "hemligt" not in stored
        assert auth.check(config, "hemligt-lösen")

    def test_it_can_be_removed_from_the_host(self, config):
        assert auth.main(["clear", str(config._path)]) == 0
        assert not auth.password_set(Config(str(config._path)))


def test_setting_the_first_password_signs_this_browser_in(client):
    assert client.post("/api/auth/password", json={"password": "hemligt-lösen"}).status_code == 200
    assert client.get("/api/status").status_code == 200


def test_a_short_password_is_refused(client):
    response = client.post("/api/auth/password", json={"password": "abc"})
    assert response.status_code == 400


def test_the_signing_key_survives_a_restart(config):
    assert auth.secret_key(config) == auth.secret_key(config)
    assert (config._path.with_name("secret_key").stat().st_mode & 0o777) == 0o600


def test_the_diagnostics_never_show_the_hash(config):
    from adr.bundle import _settings

    auth.set_password(config, "hemligt-lösen")
    text = _settings(config)
    assert config.as_dict()[auth.HASH_KEY] not in text
    assert "web_password_hash = <set, redacted>" in text


def test_a_book_being_built_is_on_every_page(config, client):
    from adr.models import Job, JobStatus, get_session, init_db

    init_db()
    session = get_session()
    session.add(Job(disc_label="Mio", title="Astrid Lindgren — Mio, min Mio", drive="audiobook",
                    content_type="audiobook", status=JobStatus.ENCODING, progress_encode=0.42))
    session.commit()
    session.close()

    page = client.get("/settings").get_data(as_text=True)
    assert "Building <strong>Astrid Lindgren — Mio, min Mio</strong>" in page
    assert "42%" in page
    assert client.get("/api/audiobook").get_json()["builds"][0]["progress"] == 0.42
