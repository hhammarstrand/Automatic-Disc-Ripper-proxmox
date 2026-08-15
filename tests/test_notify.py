"""Tests for adr.notify.

Two things matter here and both are about restraint. A notification transport
takes a URL from settings, so it must refuse anything that is not http(s). And
a notification service being down must never fail a rip that succeeded — the
film is on disk either way, and an exception here would be the worse outcome.
"""

import types

import pytest
import requests

from adr import notify


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def captured(monkeypatch):
    """Record what would have been POSTed, without leaving the machine."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    return calls


class TestUrlValidation:
    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "not-a-url",
        "",
        "http://",          # scheme but no host
        "javascript:alert(1)",
    ])
    def test_non_http_urls_are_refused(self, url, monkeypatch):
        """The URL comes from settings, which the unauthenticated UI can write."""
        def _explode(*a, **k):
            raise AssertionError("must not issue a request for a rejected URL")
        monkeypatch.setattr(notify.requests, "post", _explode)

        ok, detail = notify.send("ntfy", url, "t", "m")
        assert ok is False
        assert "http" in detail

    def test_https_is_accepted(self, captured):
        ok, _ = notify.send("ntfy", "https://ntfy.sh/topic", "t", "m")
        assert ok is True

    def test_unknown_provider_is_refused(self, monkeypatch):
        def _explode(*a, **k):
            raise AssertionError("must not issue a request for an unknown provider")
        monkeypatch.setattr(notify.requests, "post", _explode)
        ok, detail = notify.send("carrier-pigeon", "https://x.test/y", "t", "m")
        assert ok is False
        assert "carrier-pigeon" in detail


class TestProviderShapes:
    def test_ntfy_sends_the_body_as_data_with_headers(self, captured):
        notify.send("ntfy", "https://ntfy.sh/t", "Title", "Body",
                    notify.EVENT_JOB_FAILED, token="tok")
        call = captured[0]
        assert call["url"] == "https://ntfy.sh/t"
        assert call["data"] == b"Body"
        assert call["headers"]["Title"] == "Title"
        assert call["headers"]["Priority"] == "high", "a failure should not be a quiet ping"
        assert call["headers"]["Authorization"] == "Bearer tok"

    def test_ntfy_without_a_token_sends_no_auth_header(self, captured):
        notify.send("ntfy", "https://ntfy.sh/t", "T", "B")
        assert "Authorization" not in captured[0]["headers"]

    def test_gotify_posts_to_the_message_endpoint(self, captured):
        notify.send("gotify", "http://gotify.local:8080/", "T", "B",
                    notify.EVENT_JOB_DONE, token="abc")
        call = captured[0]
        assert call["url"] == "http://gotify.local:8080/message?token=abc"
        assert call["json"]["title"] == "T"
        assert call["json"]["priority"] == 4

    def test_gotify_raises_priority_for_failures(self, captured):
        notify.send("gotify", "http://g.local", "T", "B", notify.EVENT_JOB_FAILED)
        assert captured[0]["json"]["priority"] == 8

    def test_discord_uses_an_embed_coloured_by_outcome(self, captured):
        notify.send("discord", "https://discord.com/api/webhooks/1/x", "T", "B",
                    notify.EVENT_JOB_FAILED)
        embed = captured[0]["json"]["embeds"][0]
        assert embed["title"] == "T"
        assert embed["color"] == 0xE74C3C

    def test_webhook_sends_the_documented_shape(self, captured):
        notify.send("webhook", "https://hooks.test/adr", "T", "B",
                    notify.EVENT_JOB_DONE, token="s3cret")
        call = captured[0]
        assert call["json"] == {"event": "job_done", "title": "T", "message": "B"}
        assert call["headers"]["Authorization"] == "Bearer s3cret"


class TestFailureHandling:
    def test_a_timeout_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(notify.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()))
        ok, detail = notify.send("ntfy", "https://ntfy.sh/t", "T", "B")
        assert ok is False
        assert "ntfy.sh" in detail

    def test_a_connection_error_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(notify.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()))
        ok, detail = notify.send("gotify", "http://dead.local", "T", "B")
        assert ok is False
        assert "dead.local" in detail

    def test_an_http_error_includes_the_body(self, monkeypatch):
        monkeypatch.setattr(notify.requests, "post",
                            lambda *a, **k: FakeResponse(403, "forbidden topic"))
        ok, detail = notify.send("ntfy", "https://ntfy.sh/t", "T", "B")
        assert ok is False
        assert "403" in detail
        assert "forbidden topic" in detail

    def test_a_huge_error_body_is_truncated(self, monkeypatch):
        monkeypatch.setattr(notify.requests, "post",
                            lambda *a, **k: FakeResponse(500, "x" * 10_000))
        _, detail = notify.send("ntfy", "https://ntfy.sh/t", "T", "B")
        assert len(detail) < 300


def _config(**overrides):
    data = {
        "notify_enabled": True,
        "notify_provider": "ntfy",
        "notify_url": "https://ntfy.sh/topic",
        "notify_token": "",
        "notify_events": ["job_done", "job_failed"],
    }
    data.update(overrides)
    return types.SimpleNamespace(**data)


def _job(**overrides):
    data = {
        "display_title": "The Matrix (1999)",
        "error_message": None,
        "avg_fps": None,
    }
    data.update(overrides)
    return types.SimpleNamespace(**data)


class TestNotifier:
    def test_disabled_sends_nothing(self, monkeypatch):
        def _explode(*a, **k):
            raise AssertionError("must not send when disabled")
        monkeypatch.setattr(notify.requests, "post", _explode)
        assert notify.Notifier(_config(notify_enabled=False)).job_done(_job()) is False

    def test_an_empty_url_counts_as_disabled(self, monkeypatch):
        monkeypatch.setattr(notify.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
        assert notify.Notifier(_config(notify_url="")).job_done(_job()) is False

    def test_an_unselected_event_is_not_sent(self, captured):
        notifier = notify.Notifier(_config(notify_events=["job_failed"]))
        assert notifier.job_done(_job()) is False
        assert captured == []

    def test_a_selected_event_is_sent(self, captured):
        assert notify.Notifier(_config()).job_done(_job(), "/mnt/media") is True
        assert "The Matrix (1999)" in captured[0]["data"].decode()
        assert "/mnt/media" in captured[0]["data"].decode()

    def test_the_failure_message_carries_the_reason(self, captured):
        job = _job(error_message="Destination /mnt/media is not a mounted filesystem.")
        notify.Notifier(_config()).job_failed(job)
        assert "not a mounted filesystem" in captured[0]["data"].decode()

    def test_a_failure_with_no_reason_still_says_something(self, captured):
        notify.Notifier(_config()).job_failed(_job())
        assert captured[0]["data"].decode().strip()

    def test_a_dead_service_does_not_raise(self, monkeypatch):
        """This runs at the end of a successful rip. It must never throw."""
        monkeypatch.setattr(notify.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()))
        assert notify.Notifier(_config()).job_done(_job()) is False

    def test_malformed_events_mean_none_not_all(self, captured):
        """Failing open here would spam the user for a typo in the YAML."""
        notifier = notify.Notifier(_config(notify_events=None))
        assert notifier.job_done(_job()) is False
        assert notifier.job_failed(_job()) is False
        assert captured == []

    def test_disc_inserted_names_the_drive(self, captured):
        notifier = notify.Notifier(_config(notify_events=["disc_inserted"]))
        assert notifier.disc_inserted("/dev/sr0", "THE_MATRIX") is True
        body = captured[0]["data"].decode()
        assert "/dev/sr0" in body
        assert "THE_MATRIX" in body

    def test_an_unlabelled_disc_does_not_render_as_none(self, captured):
        notify.Notifier(_config(notify_events=["disc_inserted"])).disc_inserted("/dev/sr0", None)
        assert "None" not in captured[0]["data"].decode()


class TestDuplicateEvent:
    """Worth sending while the user is still standing at the drive."""

    def test_it_is_sent_when_selected(self, captured):
        notifier = notify.Notifier(_config(notify_events=["duplicate"]))
        job = _job()
        assert notifier.duplicate(job, "Already in /mnt/media/The Matrix (1999).") is True
        body = captured[0]["data"].decode()
        assert "/mnt/media/The Matrix (1999)" in body
        assert "The Matrix (1999)" in captured[0]["headers"]["Title"]

    def test_it_is_not_sent_unless_selected(self, captured):
        """It is off by default: most people do not want a push per disc."""
        notifier = notify.Notifier(_config())      # job_done + job_failed only
        assert notifier.duplicate(_job(), "detail") is False
        assert captured == []

    def test_it_is_not_a_failure_priority(self, captured):
        """A duplicate is information, not an alarm."""
        notify.Notifier(_config(notify_events=["duplicate"])).duplicate(_job(), "x")
        assert captured[0]["headers"]["Priority"] != "high"


class TestTheDriveIsNamedTheWayItsOwnerNamedIt:
    """"Saltkråkan DVD 2 in /dev/sr0" is the right sentence in a diagnostics
    bundle and the wrong one on a phone. Naming the drives — Internal,
    External — is exactly so that you do not have to remember which node is
    the one on the shelf.
    """

    def _config(self, labels, **extra):
        import types

        base = {
            "notify_enabled": True, "notify_provider": "ntfy",
            "notify_url": "https://ntfy.sh/x", "notify_token": "",
            "notify_events": ["disc_inserted"],
            "drive_display": lambda d: labels.get(d, d),
        }
        base.update(extra)
        return types.SimpleNamespace(**base)

    def _sent(self, config, drive, label):
        from adr import notify

        seen = {}
        notifier = notify.Notifier(config)
        notifier.notify = lambda event, title, message: seen.update(
            {"message": message}) or True
        notifier.disc_inserted(drive, label)
        return seen["message"]

    def test_a_named_drive_is_named(self):
        message = self._sent(
            self._config({"/dev/sr0": "Internal"}), "/dev/sr0", "Saltkråkan DVD 2")
        assert message == "Saltkråkan DVD 2 in Internal."
        assert "/dev/sr0" not in message

    def test_an_unnamed_drive_still_says_which_one(self):
        """Falling back to the node matters most when there are two of them."""
        message = self._sent(self._config({}), "/dev/sr1", "Saltkråkan DVD 1")
        assert "/dev/sr1" in message

    def test_a_config_without_the_helper_does_not_break_the_notification(self):
        """Notifier is handed plain objects in several places; a missing
        method must not cost the user the notification itself."""
        import types

        config = types.SimpleNamespace(
            notify_enabled=True, notify_provider="ntfy",
            notify_url="https://ntfy.sh/x", notify_token="",
            notify_events=["disc_inserted"],
        )
        assert "/dev/sr0" in self._sent(config, "/dev/sr0", "A disc")


class TestTheDriveNameHelper:
    def _config(self, tmp_path, labels):
        from adr.config import Config

        config = Config(str(tmp_path / "adr.yaml"))
        config.update({"drive_labels": labels})
        return config

    def test_it_prefers_the_name(self, tmp_path):
        config = self._config(tmp_path, {"/dev/sr1": "External"})
        assert config.drive_display("/dev/sr1") == "External"

    def test_it_falls_back_to_the_device(self, tmp_path):
        assert self._config(tmp_path, {}).drive_display("/dev/sr1") == "/dev/sr1"

    def test_a_blank_name_is_not_a_name(self, tmp_path):
        config = self._config(tmp_path, {"/dev/sr1": ""})
        assert config.drive_display("/dev/sr1") == "/dev/sr1"
