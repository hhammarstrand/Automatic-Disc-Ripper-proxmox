"""Ask Plex to scan the library after a film lands in it.

Without this the film exists on disk and is invisible in Plex until whenever
the next scheduled scan happens — which for most people is "sometime tonight",
and reads as the ripper having failed.

Plex's HTTP API is used directly rather than via a client library: two
endpoints, no dependency, and the failure modes stay legible. Everything is
best-effort — a Plex server that is down must not fail a rip that succeeded.

Getting the token: open any item in the Plex web UI, choose *Get Info* →
*View XML*, and copy the `X-Plex-Token` value out of the resulting URL.
"""

import logging
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 10


def _valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _get(url: str, path: str, token: str) -> requests.Response:
    return requests.get(
        url.rstrip("/") + path,
        headers={"X-Plex-Token": token, "Accept": "application/xml"},
        timeout=TIMEOUT,
    )


def list_sections(url: str, token: str) -> tuple[list[dict], str]:
    """The libraries this server has. Returns ``(sections, error)``.

    Used by the settings page so the user picks a library from a list instead
    of guessing a numeric section key.
    """
    if not _valid_url(url):
        return [], "The Plex URL must be a full http:// or https:// address."
    if not token:
        return [], "A Plex token is required."

    try:
        response = _get(url, "/library/sections", token)
    except requests.Timeout:
        return [], f"No answer from {urlparse(url).netloc} within {TIMEOUT}s."
    except requests.ConnectionError:
        return [], f"Could not reach {urlparse(url).netloc}."
    except requests.RequestException as exc:
        return [], f"Request failed: {exc}"

    if response.status_code == 401:
        return [], "Plex rejected the token (HTTP 401). Check X-Plex-Token."
    if response.status_code >= 400:
        return [], f"Plex returned HTTP {response.status_code}."

    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError:
        return [], "Plex returned something that is not XML — is this really a Plex server?"

    sections = [
        {
            "key": directory.get("key", ""),
            "title": directory.get("title", ""),
            "type": directory.get("type", ""),
        }
        for directory in root.findall(".//Directory")
        if directory.get("key")
    ]
    if not sections:
        return [], "Plex answered, but reported no libraries."
    return sections, ""


def refresh_section(url: str, token: str, section: str,
                    path: str = "") -> tuple[bool, str]:
    """Trigger a scan. Returns ``(ok, detail)``; never raises.

    With *path*, Plex is asked to scan just that folder, which on a large
    library is the difference between seconds and a full walk of every file.
    """
    if not _valid_url(url):
        return False, "The Plex URL must be a full http:// or https:// address."
    if not str(section).strip():
        return False, "No Plex library selected."

    endpoint = f"/library/sections/{section}/refresh"
    params = {}
    if path:
        params["path"] = path

    try:
        response = requests.get(
            url.rstrip("/") + endpoint,
            headers={"X-Plex-Token": token},
            params=params,
            timeout=TIMEOUT,
        )
    except requests.Timeout:
        return False, f"No answer from {urlparse(url).netloc} within {TIMEOUT}s."
    except requests.ConnectionError:
        return False, f"Could not reach {urlparse(url).netloc}."
    except requests.RequestException as exc:
        return False, f"Request failed: {exc}"

    if response.status_code == 401:
        return False, "Plex rejected the token (HTTP 401)."
    if response.status_code >= 400:
        return False, f"Plex returned HTTP {response.status_code}."
    return True, (
        f"Plex is scanning {path}." if path else "Plex is scanning the library."
    )


class PlexNotifier:
    """Refreshes the configured library, and stays quiet when not configured."""

    def __init__(self, config):
        self._config = config

    @property
    def enabled(self) -> bool:
        return bool(
            self._config.plex_refresh_enabled
            and self._config.plex_url
            and self._config.plex_token
            and self._config.plex_section
        )

    def refresh_for(self, output_path: str = "") -> bool:
        """Scan the folder a film just landed in. Returns whether Plex accepted."""
        if not self.enabled:
            return False
        ok, detail = refresh_section(
            self._config.plex_url,
            self._config.plex_token,
            self._config.plex_section,
            path=output_path or "",
        )
        if ok:
            logger.info("Plex refresh requested: %s", detail)
        else:
            # The film is on disk; Plex will find it on its own schedule. This
            # is a convenience, not a step the rip depends on.
            logger.warning("Plex refresh failed: %s", detail)
        return ok
