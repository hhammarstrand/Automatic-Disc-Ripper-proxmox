"""Tests for adr.plex.

The refresh runs at the end of a successful rip, so the governing rule is that
a Plex server which is down, wrong, or not actually Plex must produce a legible
message and never an exception.
"""

import types

import pytest
import requests

from adr import plex

SECTIONS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="2">
  <Directory key="1" type="movie" title="Movies"/>
  <Directory key="2" type="show" title="TV Shows"/>
</MediaContainer>"""


class FakeResponse:
    def __init__(self, status_code=200, content=b"", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text


class TestListSections:
    def test_libraries_are_parsed(self, monkeypatch):
        monkeypatch.setattr(plex.requests, "get",
                            lambda *a, **k: FakeResponse(200, SECTIONS_XML))
        sections, error = plex.list_sections("http://plex.local:32400", "tok")
        assert error == ""
        assert sections == [
            {"key": "1", "type": "movie", "title": "Movies"},
            {"key": "2", "type": "show", "title": "TV Shows"},
        ]

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "not-a-url", ""])
    def test_a_bad_url_is_refused_before_any_request(self, url, monkeypatch):
        def _explode(*a, **k):
            raise AssertionError("must not issue a request for a rejected URL")
        monkeypatch.setattr(plex.requests, "get", _explode)
        sections, error = plex.list_sections(url, "tok")
        assert sections == []
        assert "http" in error

    def test_a_missing_token_is_refused(self):
        sections, error = plex.list_sections("http://plex.local:32400", "")
        assert sections == []
        assert "token" in error.lower()

    def test_a_rejected_token_says_so(self, monkeypatch):
        monkeypatch.setattr(plex.requests, "get", lambda *a, **k: FakeResponse(401))
        _, error = plex.list_sections("http://plex.local:32400", "wrong")
        assert "401" in error
        assert "token" in error.lower()

    def test_something_that_is_not_plex_says_so(self, monkeypatch):
        """Pointing this at a random web server should not look like a Plex error."""
        monkeypatch.setattr(plex.requests, "get",
                            lambda *a, **k: FakeResponse(200, b"<html>hello</html>"))
        _, error = plex.list_sections("http://nginx.local", "tok")
        assert "not XML" in error or "no libraries" in error

    def test_an_empty_server_is_reported_as_empty_not_broken(self, monkeypatch):
        monkeypatch.setattr(plex.requests, "get", lambda *a, **k: FakeResponse(
            200, b'<?xml version="1.0"?><MediaContainer size="0"/>'))
        sections, error = plex.list_sections("http://plex.local", "tok")
        assert sections == []
        assert "no libraries" in error

    def test_a_timeout_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(plex.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()))
        _, error = plex.list_sections("http://plex.local", "tok")
        assert "plex.local" in error


class TestRefreshSection:
    def _capture(self, monkeypatch):
        calls = []

        def fake_get(url, **kwargs):
            calls.append({"url": url, **kwargs})
            return FakeResponse(200)

        monkeypatch.setattr(plex.requests, "get", fake_get)
        return calls

    def test_the_refresh_endpoint_is_called(self, monkeypatch):
        calls = self._capture(monkeypatch)
        ok, _ = plex.refresh_section("http://plex.local:32400", "tok", "1")
        assert ok is True
        assert calls[0]["url"] == "http://plex.local:32400/library/sections/1/refresh"
        assert calls[0]["headers"]["X-Plex-Token"] == "tok"

    def test_a_path_narrows_the_scan(self, monkeypatch):
        """On a large library this is the difference between seconds and minutes."""
        calls = self._capture(monkeypatch)
        _, detail = plex.refresh_section(
            "http://plex.local:32400", "tok", "1", path="/mnt/media/The Matrix (1999)")
        assert calls[0]["params"]["path"] == "/mnt/media/The Matrix (1999)"
        assert "The Matrix" in detail

    def test_no_path_scans_the_whole_library(self, monkeypatch):
        calls = self._capture(monkeypatch)
        plex.refresh_section("http://plex.local", "tok", "1")
        assert calls[0]["params"] == {}

    def test_a_missing_section_is_refused(self, monkeypatch):
        def _explode(*a, **k):
            raise AssertionError("must not request a refresh with no section")
        monkeypatch.setattr(plex.requests, "get", _explode)
        ok, detail = plex.refresh_section("http://plex.local", "tok", "")
        assert ok is False
        assert "library" in detail.lower()

    def test_a_connection_error_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(plex.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()))
        ok, detail = plex.refresh_section("http://plex.local", "tok", "1")
        assert ok is False
        assert "plex.local" in detail


def _config(**overrides):
    data = {
        "plex_refresh_enabled": True,
        "plex_url": "http://plex.local:32400",
        "plex_token": "tok",
        "plex_section": "1",
    }
    data.update(overrides)
    return types.SimpleNamespace(**data)


class TestPlexNotifier:
    def test_disabled_does_nothing(self, monkeypatch):
        def _explode(*a, **k):
            raise AssertionError("must not call Plex when disabled")
        monkeypatch.setattr(plex.requests, "get", _explode)
        assert plex.PlexNotifier(_config(plex_refresh_enabled=False)).refresh_for("/x") is False

    @pytest.mark.parametrize("missing", ["plex_url", "plex_token", "plex_section"])
    def test_incomplete_configuration_counts_as_disabled(self, missing, monkeypatch):
        monkeypatch.setattr(plex.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
        assert plex.PlexNotifier(_config(**{missing: ""})).refresh_for("/x") is False

    def test_enabled_asks_plex_to_scan_the_new_folder(self, monkeypatch):
        calls = []
        monkeypatch.setattr(plex.requests, "get",
                            lambda url, **k: (calls.append(k), FakeResponse(200))[1])
        assert plex.PlexNotifier(_config()).refresh_for("/mnt/media/Heat (1995)") is True
        assert calls[0]["params"]["path"] == "/mnt/media/Heat (1995)"

    def test_a_dead_plex_does_not_raise(self, monkeypatch):
        """This runs after a successful rip. The film is on disk regardless."""
        monkeypatch.setattr(plex.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()))
        assert plex.PlexNotifier(_config()).refresh_for("/x") is False
