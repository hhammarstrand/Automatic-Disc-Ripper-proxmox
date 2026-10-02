"""An optional password in front of the web UI.

The dashboard was open to anyone who could reach it, which on a home LAN was a
defensible choice while it could only rip discs. It can now read the media
share, start jobs on files there, and delete things — and a guest on the Wi-Fi
is somebody who can reach it. So there is a password, off until one is set.

What is stored is a salted hash (werkzeug's), never the password. The session
cookie is signed with a key kept beside the config, so it survives restarts
and updates; it carries a fingerprint of the hash, so changing or removing the
password signs every browser out.

Forgotten it? On the Proxmox host::

    pct exec <CTID> -- /opt/adr/.venv/bin/python -m adr.auth clear

which removes the password and leaves everything else alone.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

HASH_KEY = "web_password_hash"
MIN_LENGTH = 6

# Five wrong guesses from one address in five minutes, then nothing for the
# rest of the five minutes. A home LAN is not the internet, but a password
# that can be tried as fast as a script can type is not a password.
MAX_FAILURES = 5
FAILURE_WINDOW = 300
_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def password_set(config) -> bool:
    return bool(config.as_dict().get(HASH_KEY))


def fingerprint(config) -> str:
    """What a signed-in session carries: changes whenever the password does."""
    stored = config.as_dict().get(HASH_KEY) or ""
    return hashlib.sha256(stored.encode()).hexdigest()[:16]


def set_password(config, password: str) -> None:
    from werkzeug.security import generate_password_hash

    if len(password or "") < MIN_LENGTH:
        raise ValueError(f"A password needs at least {MIN_LENGTH} characters.")
    config.update({HASH_KEY: generate_password_hash(password)})
    logger.info("Web UI password set")


def clear_password(config) -> None:
    config.update({HASH_KEY: ""})
    logger.info("Web UI password removed")


def check(config, password: str) -> bool:
    from werkzeug.security import check_password_hash

    stored = config.as_dict().get(HASH_KEY) or ""
    return bool(stored) and check_password_hash(stored, password or "")


def locked_out(address: str) -> bool:
    now = time.monotonic()
    with _failures_lock:
        recent = [t for t in _failures.get(address, []) if now - t < FAILURE_WINDOW]
        _failures[address] = recent
        return len(recent) >= MAX_FAILURES


def record_failure(address: str) -> None:
    with _failures_lock:
        _failures.setdefault(address, []).append(time.monotonic())


def clear_failures(address: str) -> None:
    with _failures_lock:
        _failures.pop(address, None)


def secret_key(config) -> str:
    """The key that signs session cookies, created once beside the config."""
    path = Path(getattr(config, "_path", "adr.yaml")).with_name("secret_key")
    try:
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        path.write_text(key, encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        logger.warning("Could not save %s; sessions end at the next restart", path)
    return key


def main(argv: list[str] | None = None) -> int:
    from adr.config import Config

    args = list(sys.argv[1:] if argv is None else argv)
    config = Config(args[1]) if len(args) > 1 else Config()
    if args[:1] == ["clear"]:
        clear_password(config)
        print("The web UI password has been removed. Set a new one under Settings.")
        return 0
    print("Usage: python -m adr.auth clear [path/to/adr.yaml]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
