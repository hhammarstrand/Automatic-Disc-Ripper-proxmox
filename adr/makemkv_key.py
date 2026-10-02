"""MakeMKV registration-key management.

MakeMKV on Linux needs a registration key in ~/.MakeMKV/settings.conf. While
MakeMKV is in beta the developer posts a free key on the forum that rotates
roughly monthly:

    https://forum.makemkv.com/forum/viewtopic.php?t=1053

This module can:

- Fetch the current free beta key from that thread.
- Write it to ~/.MakeMKV/settings.conf as `app_Key = "T-..."`.
- Accept an explicit key (a purchased key, or one supplied via the
  ADR_MAKEMKV_KEY environment variable) and prefer it over the beta key.

It is safe to call repeatedly; the file is only rewritten when the key changes.
The CLI entry point (``python -m adr.makemkv_key``) is used by the installer.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

FORUM_URL = "https://forum.makemkv.com/forum/viewtopic.php?t=1053"
# Beta keys look like  T-<~64 base64-ish chars>.  Validate strictly so a
# compromised/garbage forum page can never inject an arbitrary string.
KEY_RE = re.compile(r"T-[A-Za-z0-9@_+\-]{60,70}")

SETTINGS_DIR = Path.home() / ".MakeMKV"
SETTINGS_FILE = SETTINGS_DIR / "settings.conf"


def fetch_latest_key(timeout: int = 15) -> str | None:
    """Scrape the MakeMKV forum for the latest beta key. Returns None on failure."""
    try:
        resp = requests.get(FORUM_URL, timeout=timeout, headers={"User-Agent": "adr/1.0"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("MakeMKV forum fetch failed: %s", exc)
        return None
    matches = KEY_RE.findall(resp.text)
    if not matches:
        logger.warning("No beta key (T-...) found in forum HTML")
        return None
    # The key is posted inside a <code> block near the top; the first strict
    # match is the current key.
    return matches[0]


def is_valid_key(key: str) -> bool:
    """True if `key` looks like a valid MakeMKV registration key."""
    return bool(key) and KEY_RE.fullmatch(key.strip()) is not None


def write_key(key: str, path: Path = SETTINGS_FILE) -> None:
    """Write `key` to settings.conf (0600), only if it changed."""
    key = key.strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        logger.debug("Could not chmod %s", path.parent, exc_info=True)

    new_contents = f'app_Key = "{key}"\n'
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == new_contents:
                logger.info("MakeMKV key unchanged (%s)", path)
                return
        except OSError:
            pass
    path.write_text(new_contents, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.debug("Could not chmod %s", path, exc_info=True)
    logger.info("Wrote MakeMKV key to %s", path)


def read_existing_key(path: Path = SETTINGS_FILE) -> str | None:
    """Return the key already stored in settings.conf, or None."""
    if not path.exists():
        return None
    try:
        m = KEY_RE.search(path.read_text(encoding="utf-8"))
        return m.group(0) if m else None
    except OSError:
        return None


def ensure_key(explicit_key: str | None = None, path: Path = SETTINGS_FILE) -> str | None:
    """Best-effort: make sure a valid key is present in settings.conf.

    Precedence:
      1. explicit_key argument (e.g. from the web UI)
      2. ADR_MAKEMKV_KEY environment variable
      3. an existing valid key already in settings.conf
      4. a freshly fetched beta key from the forum

    Returns the resulting key, or None if none could be obtained.
    """
    candidate = (explicit_key or os.environ.get("ADR_MAKEMKV_KEY") or "").strip()
    if candidate:
        if is_valid_key(candidate):
            write_key(candidate, path)
            return candidate
        logger.warning("Supplied MakeMKV key is malformed — ignoring")

    existing = read_existing_key(path)
    if existing:
        return existing

    fetched = fetch_latest_key()
    if fetched and is_valid_key(fetched):
        write_key(fetched, path)
        return fetched
    return None


# The forum is somebody else's server. A drive that keeps rejecting discs must
# not turn into a request per disc, so a fetch that has just been tried is not
# tried again for a while — whether it worked or not.
FETCH_INTERVAL = 600
_fetch_lock = threading.Lock()
_last_fetch = 0.0


def _fetch_throttled() -> str | None:
    global _last_fetch
    with _fetch_lock:
        now = time.monotonic()
        if _last_fetch and now - _last_fetch < FETCH_INTERVAL:
            logger.info("MakeMKV key fetched %ds ago; not asking the forum again yet",
                        now - _last_fetch)
            return None
        _last_fetch = now
    key = fetch_latest_key()
    return key if key and is_valid_key(key) else None


def ensure_present(path: Path = SETTINGS_FILE) -> str | None:
    """Make sure *some* key is stored, fetching one only when there is none.

    Called before every scan and rip. Without a key MakeMKV refuses the disc,
    and the fix — the free beta key the forum posts — needs nobody to press
    anything.
    """
    existing = read_existing_key(path)
    if existing:
        return existing
    explicit = (os.environ.get("ADR_MAKEMKV_KEY") or "").strip()
    if is_valid_key(explicit):
        write_key(explicit, path)
        return explicit
    logger.warning("No MakeMKV key stored; fetching the current beta key")
    fetched = _fetch_throttled()
    if fetched:
        write_key(fetched, path)
    return fetched


def refresh_key(path: Path = SETTINGS_FILE, throttle: bool = True) -> str | None:
    """Replace the stored key with the forum's current one.

    For a key MakeMKV has rejected: the beta key rotates about monthly, and an
    old one stays in settings.conf looking perfectly valid. ensure_key keeps an
    existing key by design, which is why it cannot be the answer to an expired
    one. A key from ADR_MAKEMKV_KEY was chosen on purpose and is left alone.

    Returns the key now stored — the new one, or the old one when the forum
    could not be reached.
    """
    if is_valid_key((os.environ.get("ADR_MAKEMKV_KEY") or "").strip()):
        return ensure_key(path=path)
    fetched = _fetch_throttled() if throttle else fetch_latest_key()
    if fetched and is_valid_key(fetched):
        if fetched != read_existing_key(path):
            logger.info("MakeMKV key replaced with the current beta key")
        write_key(fetched, path)
        return fetched
    return read_existing_key(path)


def key_rejected(output: str) -> bool:
    """Whether MakeMKV's output says its registration key was refused."""
    return "registration key" in output.lower() or "app_KeyExpired" in output


def ensure_in_background() -> None:
    """Fetch a missing key without holding up startup on the network."""
    threading.Thread(target=_ensure_quietly, name="MakeMKVKey", daemon=True).start()


def _ensure_quietly() -> None:
    try:
        if not ensure_present():
            logger.warning("No MakeMKV key, and none could be fetched; "
                           "retrying before the next disc")
    except Exception:                                   # noqa: BLE001 - logged
        logger.exception("Could not ensure a MakeMKV key")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Fetch/write the MakeMKV registration key.")
    parser.add_argument("--fetch", action="store_true", help="Fetch the latest beta key from the forum")
    parser.add_argument("--write", action="store_true", help="Write the key to ~/.MakeMKV/settings.conf")
    parser.add_argument("--key", help="Use this explicit key instead of fetching")
    parser.add_argument("--ensure", action="store_true",
                        help="Ensure a key exists (env > existing > forum) and write it")
    args = parser.parse_args(argv)

    if args.ensure:
        key = ensure_key(args.key)
        if not key:
            logger.error("Could not obtain a MakeMKV key")
            return 1
        print(key)
        return 0

    key = args.key
    if key and not is_valid_key(key):
        logger.error("Supplied key is malformed")
        return 1
    if not key and args.fetch:
        key = fetch_latest_key()
    if not key:
        logger.error("No key available")
        return 1
    if args.write:
        write_key(key)
    print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
