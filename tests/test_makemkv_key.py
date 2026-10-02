"""Tests for adr.makemkv_key (beta-key fetch + settings.conf writing)."""

import pytest

from adr import makemkv_key

VALID_KEY = "T-" + "a" * 64
ANOTHER_KEY = "T-" + "b" * 64


# ------------------------------------------------------------------ #
# is_valid_key
# ------------------------------------------------------------------ #

class TestIsValidKey:
    def test_valid(self):
        assert makemkv_key.is_valid_key(VALID_KEY)

    def test_valid_with_special_chars(self):
        assert makemkv_key.is_valid_key("T-" + "aB3@_+-" * 9 + "abc")

    def test_too_short(self):
        assert not makemkv_key.is_valid_key("T-abc")

    def test_no_prefix(self):
        assert not makemkv_key.is_valid_key("X-" + "a" * 64)

    def test_empty(self):
        assert not makemkv_key.is_valid_key("")


# ------------------------------------------------------------------ #
# write_key / read_existing_key
# ------------------------------------------------------------------ #

class TestWriteReadKey:
    def test_write_then_read(self, tmp_path):
        path = tmp_path / "settings.conf"
        makemkv_key.write_key(VALID_KEY, path)
        assert path.read_text() == f'app_Key = "{VALID_KEY}"\n'
        assert makemkv_key.read_existing_key(path) == VALID_KEY

    def test_write_sets_mode_600(self, tmp_path):
        path = tmp_path / "settings.conf"
        makemkv_key.write_key(VALID_KEY, path)
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_read_missing(self, tmp_path):
        assert makemkv_key.read_existing_key(tmp_path / "nope.conf") is None


# ------------------------------------------------------------------ #
# fetch_latest_key
# ------------------------------------------------------------------ #

class TestFetchLatestKey:
    def test_extracts_key_from_html(self, monkeypatch):
        html = f"<html><code>{VALID_KEY}</code> blah</html>"
        monkeypatch.setattr(
            makemkv_key.requests, "get",
            lambda *a, **k: _FakeResp(html),
        )
        assert makemkv_key.fetch_latest_key() == VALID_KEY

    def test_no_key_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            makemkv_key.requests, "get",
            lambda *a, **k: _FakeResp("<html>nothing here</html>"),
        )
        assert makemkv_key.fetch_latest_key() is None

    def test_network_error_returns_none(self, monkeypatch):
        def _raise(*a, **k):
            raise makemkv_key.requests.RequestException("offline")

        monkeypatch.setattr(makemkv_key.requests, "get", _raise)
        assert makemkv_key.fetch_latest_key() is None


# ------------------------------------------------------------------ #
# ensure_key precedence
# ------------------------------------------------------------------ #

class TestEnsureKey:
    def test_explicit_key_wins(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ADR_MAKEMKV_KEY", raising=False)
        path = tmp_path / "settings.conf"
        assert makemkv_key.ensure_key(VALID_KEY, path) == VALID_KEY
        assert makemkv_key.read_existing_key(path) == VALID_KEY

    def test_env_var_used(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ADR_MAKEMKV_KEY", ANOTHER_KEY)
        path = tmp_path / "settings.conf"
        assert makemkv_key.ensure_key(None, path) == ANOTHER_KEY

    def test_existing_key_reused(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ADR_MAKEMKV_KEY", raising=False)
        path = tmp_path / "settings.conf"
        makemkv_key.write_key(VALID_KEY, path)
        # No network call should be needed
        monkeypatch.setattr(makemkv_key, "fetch_latest_key", lambda *a, **k: pytest.fail("should not fetch"))
        assert makemkv_key.ensure_key(None, path) == VALID_KEY

    def test_falls_back_to_fetch(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ADR_MAKEMKV_KEY", raising=False)
        path = tmp_path / "settings.conf"
        monkeypatch.setattr(makemkv_key, "fetch_latest_key", lambda *a, **k: ANOTHER_KEY)
        assert makemkv_key.ensure_key(None, path) == ANOTHER_KEY
        assert makemkv_key.read_existing_key(path) == ANOTHER_KEY

    def test_malformed_explicit_ignored_then_fetch(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ADR_MAKEMKV_KEY", raising=False)
        path = tmp_path / "settings.conf"
        monkeypatch.setattr(makemkv_key, "fetch_latest_key", lambda *a, **k: VALID_KEY)
        assert makemkv_key.ensure_key("garbage", path) == VALID_KEY

    def test_nothing_available_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ADR_MAKEMKV_KEY", raising=False)
        path = tmp_path / "settings.conf"
        monkeypatch.setattr(makemkv_key, "fetch_latest_key", lambda *a, **k: None)
        assert makemkv_key.ensure_key(None, path) is None


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


# ------------------------------------------------------------------ #
# Fetched without anybody pressing anything
# ------------------------------------------------------------------ #

class TestEnsurePresent:
    def test_a_missing_key_is_fetched(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ADR_MAKEMKV_KEY", raising=False)
        monkeypatch.setattr(makemkv_key, "fetch_latest_key", lambda *a, **k: VALID_KEY)
        path = tmp_path / "settings.conf"
        assert makemkv_key.ensure_present(path) == VALID_KEY
        assert makemkv_key.read_existing_key(path) == VALID_KEY

    def test_a_stored_key_does_not_touch_the_network(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.conf"
        makemkv_key.write_key(VALID_KEY, path)
        monkeypatch.setattr(makemkv_key, "fetch_latest_key",
                            lambda *a, **k: pytest.fail("should not fetch"))
        assert makemkv_key.ensure_present(path) == VALID_KEY

    def test_the_forum_is_not_asked_on_every_disc(self, tmp_path, monkeypatch):
        """A forum that is down must not become one request per disc."""
        monkeypatch.delenv("ADR_MAKEMKV_KEY", raising=False)
        calls = []
        monkeypatch.setattr(makemkv_key, "fetch_latest_key",
                            lambda *a, **k: calls.append(1))
        path = tmp_path / "settings.conf"
        assert makemkv_key.ensure_present(path) is None
        assert makemkv_key.ensure_present(path) is None
        assert len(calls) == 1


class TestRefreshKey:
    def test_an_expired_key_is_replaced(self, tmp_path, monkeypatch):
        """ensure_key keeps a stored key, however expired; refresh must not."""
        monkeypatch.delenv("ADR_MAKEMKV_KEY", raising=False)
        path = tmp_path / "settings.conf"
        makemkv_key.write_key(VALID_KEY, path)
        monkeypatch.setattr(makemkv_key, "fetch_latest_key", lambda *a, **k: ANOTHER_KEY)
        assert makemkv_key.refresh_key(path) == ANOTHER_KEY
        assert makemkv_key.read_existing_key(path) == ANOTHER_KEY

    def test_an_unreachable_forum_keeps_the_old_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ADR_MAKEMKV_KEY", raising=False)
        path = tmp_path / "settings.conf"
        makemkv_key.write_key(VALID_KEY, path)
        monkeypatch.setattr(makemkv_key, "fetch_latest_key", lambda *a, **k: None)
        assert makemkv_key.refresh_key(path) == VALID_KEY

    def test_a_key_chosen_on_purpose_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ADR_MAKEMKV_KEY", VALID_KEY)
        monkeypatch.setattr(makemkv_key, "fetch_latest_key", lambda *a, **k: ANOTHER_KEY)
        path = tmp_path / "settings.conf"
        assert makemkv_key.refresh_key(path) == VALID_KEY


@pytest.mark.parametrize("output, rejected", [
    ('MSG:5021,0,0,"Registration key has expired","%1"', True),
    ("MSG:1005,0,0,app_KeyExpired", True),
    ('MSG:1005,0,1,"MakeMKV v1.17 started"', False),
])
def test_key_rejected(output, rejected):
    assert makemkv_key.key_rejected(output) is rejected


class TestTheScanFetchesANewKey:
    def test_a_rejected_key_is_refreshed_before_the_retry(self, tmp_path, monkeypatch):
        import subprocess

        from adr import ripper as ripper_mod
        from adr.config import Config

        cfg = tmp_path / "adr.yaml"
        cfg.write_text(f"raw_path: {tmp_path / 'raw'}\n")
        rip = ripper_mod.MakeMKVRipper(Config(str(cfg)))
        monkeypatch.setattr(ripper_mod, "SCAN_RETRY_DELAY", 0)
        monkeypatch.setattr(makemkv_key, "ensure_present", lambda *a, **k: VALID_KEY)
        stored = {"key": VALID_KEY}
        monkeypatch.setattr(makemkv_key, "read_existing_key", lambda *a, **k: stored["key"])

        def refresh(*_a, **_k):
            stored["key"] = ANOTHER_KEY
            return ANOTHER_KEY
        monkeypatch.setattr(makemkv_key, "refresh_key", refresh)

        def scan(cmd, job_id):
            out = ('MSG:5021,0,0,"Registration key has expired","%1"\n'
                   if stored["key"] == VALID_KEY else
                   'TINFO:0,2,0,"Film"\nTINFO:0,9,0,"1:30:00"\n')
            return subprocess.CompletedProcess(cmd, 0, out, "")
        monkeypatch.setattr(rip, "_run_scan", scan)

        logged = []
        rip.log_sink = logged.append
        assert rip.scan_disc("/dev/sr0")
        assert any("fetched the current beta key" in m for m in logged)
