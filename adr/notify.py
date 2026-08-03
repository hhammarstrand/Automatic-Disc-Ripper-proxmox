"""Tell someone when a disc has finished — or failed.

The pipeline is meant to be unattended: put a disc in, walk away. Without
notifications the only way to learn that a rip failed forty minutes ago is to
open the dashboard and look, which defeats the point of walking away.

Four transports, because homelabs differ and none of them is universal:

    ntfy      push to a phone, self-hosted or ntfy.sh, no account needed
    gotify    the same idea for people who already run Gotify
    discord   a webhook URL pasted from a channel's settings
    webhook   raw JSON POST for anything else (Home Assistant, n8n, a script)

Everything is best-effort and short-timeout. A notification service being down
must never hold up a rip or fail a job — the film is on disk either way, and an
exception here would be a worse outcome than a missed message.
"""

import json
import logging
from typing import Any
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 10

# What a notification is about. Kept as plain strings because they end up in
# YAML, in JSON payloads, and in the UI.
EVENT_JOB_DONE = "job_done"
EVENT_JOB_FAILED = "job_failed"
EVENT_DISC_INSERTED = "disc_inserted"
EVENT_TEST = "test"

EVENTS = (EVENT_JOB_DONE, EVENT_JOB_FAILED, EVENT_DISC_INSERTED)

PROVIDERS = ("ntfy", "gotify", "discord", "webhook")

# ntfy renders these; the others ignore them harmlessly.
_PRIORITY = {
    EVENT_JOB_FAILED: "high",
    EVENT_JOB_DONE: "default",
    EVENT_DISC_INSERTED: "low",
    EVENT_TEST: "default",
}
_TAG = {
    EVENT_JOB_FAILED: "rotating_light",
    EVENT_JOB_DONE: "white_check_mark",
    EVENT_DISC_INSERTED: "cd",
    EVENT_TEST: "bell",
}


def _valid_url(url: str) -> bool:
    """Only http(s), and only with a host.

    The URL comes from settings, which the web UI can write, so this is the
    place to refuse file:// and friends rather than hand an arbitrary scheme to
    requests.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _payload_ntfy(url: str, title: str, message: str, event: str,
                  token: str) -> tuple[str, dict, dict]:
    headers = {
        "Title": title,
        "Priority": _PRIORITY.get(event, "default"),
        "Tags": _TAG.get(event, "bell"),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return url, {"data": message.encode("utf-8")}, headers


def _payload_gotify(url: str, title: str, message: str, event: str,
                    token: str) -> tuple[str, dict, dict]:
    # Gotify takes the application token as a query parameter and the message
    # as JSON. Priority is numeric here, unlike ntfy.
    target = url.rstrip("/") + "/message"
    if token:
        target += f"?token={token}"
    priority = 8 if event == EVENT_JOB_FAILED else 4
    return target, {"json": {"title": title, "message": message, "priority": priority}}, {}


def _payload_discord(url: str, title: str, message: str, event: str,
                     token: str) -> tuple[str, dict, dict]:
    colour = 0xE74C3C if event == EVENT_JOB_FAILED else 0x2ECC71
    return url, {"json": {"embeds": [{
        "title": title,
        "description": message,
        "color": colour,
    }]}}, {}


def _payload_webhook(url: str, title: str, message: str, event: str,
                     token: str) -> tuple[str, dict, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return url, {"json": {"event": event, "title": title, "message": message}}, headers


_BUILDERS = {
    "ntfy": _payload_ntfy,
    "gotify": _payload_gotify,
    "discord": _payload_discord,
    "webhook": _payload_webhook,
}


def send(provider: str, url: str, title: str, message: str,
         event: str = EVENT_TEST, token: str = "") -> tuple[bool, str]:
    """Deliver one notification. Returns ``(ok, detail)``; never raises.

    *detail* is what the user sees when they press Test, so it has to say what
    actually went wrong rather than "failed".
    """
    provider = (provider or "").strip().lower()
    if provider not in _BUILDERS:
        return False, f"Unknown notification type '{provider}'."
    if not _valid_url(url):
        return False, "The notification URL must be a full http:// or https:// address."

    target, kwargs, headers = _BUILDERS[provider](url, title, message, event, token)
    try:
        response = requests.post(target, headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.Timeout:
        return False, f"No answer from {urlparse(url).netloc} within {TIMEOUT}s."
    except requests.ConnectionError as exc:
        return False, f"Could not reach {urlparse(url).netloc}: {exc.__class__.__name__}"
    except requests.RequestException as exc:
        return False, f"Request failed: {exc}"

    if response.status_code >= 400:
        body = (response.text or "").strip()[:200]
        return False, f"HTTP {response.status_code}{': ' + body if body else ''}"
    return True, f"Delivered (HTTP {response.status_code})."


class Notifier:
    """Sends the events a config has enabled, and stays quiet otherwise."""

    def __init__(self, config):
        self._config = config

    @property
    def enabled(self) -> bool:
        return bool(
            self._config.notify_enabled
            and self._config.notify_url
            and self._config.notify_provider in PROVIDERS
        )

    def _should_send(self, event: str) -> bool:
        return self.enabled and event in (self._config.notify_events or [])

    def notify(self, event: str, title: str, message: str) -> bool:
        """Send *event* if it is enabled. Returns whether anything was sent."""
        if not self._should_send(event):
            return False
        ok, detail = send(
            self._config.notify_provider,
            self._config.notify_url,
            title, message, event,
            self._config.notify_token,
        )
        if ok:
            logger.info("Notification sent (%s): %s", event, title)
        else:
            # A dead notification service must never fail a job — the film is
            # on disk regardless, and this is the least important thing here.
            logger.warning("Notification failed (%s): %s", event, detail)
        return ok

    # -------------------------------------------------------------- #
    # The events themselves
    # -------------------------------------------------------------- #

    def job_done(self, job, destination: str = "") -> bool:
        parts = [f"{job.display_title} is ready."]
        if destination:
            parts.append(f"Saved to {destination}")
        if job.avg_fps:
            parts.append(f"Encoded at {job.avg_fps:.0f} fps average.")
        return self.notify(EVENT_JOB_DONE, "Disc ripped", " ".join(parts))

    def job_failed(self, job) -> bool:
        reason = (job.error_message or "No reason recorded.").strip()
        return self.notify(
            EVENT_JOB_FAILED,
            f"Rip failed: {job.display_title}",
            reason,
        )

    def disc_inserted(self, drive: str, label: str | None) -> bool:
        return self.notify(
            EVENT_DISC_INSERTED,
            "Disc inserted",
            f"{label or 'Unlabelled disc'} in {drive}.",
        )


def describe_payload(provider: str) -> dict[str, Any]:
    """An example of what a receiver will get. Shown next to the webhook field.

    Only meaningful for the raw webhook — the others have their own documented
    shapes and the user is not writing the receiver.
    """
    if provider == "webhook":
        return json.loads(json.dumps({
            "event": EVENT_JOB_DONE,
            "title": "Disc ripped",
            "message": "The Matrix (1999) is ready. Saved to /mnt/media.",
        }))
    return {}
