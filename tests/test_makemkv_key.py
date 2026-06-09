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
