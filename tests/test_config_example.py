"""The shipped example config must not drift from the defaults.

install-container.sh copies adr.yaml.example to adr.yaml on a fresh install, so
a key that exists in _DEFAULTS but not in the example is a setting no new user
can discover, and a key in the example that no longer exists is one that
silently does nothing.
"""

from pathlib import Path

import yaml

from adr.config import _DEFAULTS

EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "adr.yaml.example"


def _example() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")) or {}


def test_the_example_exists_and_parses():
    assert EXAMPLE.exists(), f"{EXAMPLE} is shipped and copied on install"
    assert isinstance(_example(), dict)


def test_every_default_appears_in_the_example():
    missing = sorted(set(_DEFAULTS) - set(_example()))
    assert not missing, (
        f"settings a new user cannot discover: {missing}. "
        "Add them to config/adr.yaml.example."
    )


def test_the_example_has_no_settings_that_do_not_exist():
    extra = sorted(set(_example()) - set(_DEFAULTS))
    assert not extra, f"keys in the example that the app ignores: {extra}"


def test_the_example_ships_no_secrets():
    """It is committed to a public repository."""
    example = _example()
    for key in ("tmdb_api_key", "notify_token", "plex_token", "notify_url", "plex_url"):
        assert example.get(key, "") == "", f"{key} must ship empty"


def test_values_match_the_defaults():
    """A different value in the example is a second, competing default."""
    example = _example()
    mismatched = {
        key: (default, example[key])
        for key, default in _DEFAULTS.items()
        # Paths serialise as strings; compare them that way.
        if key in example and str(example[key]) != str(default)
    }
    assert not mismatched, f"example disagrees with _DEFAULTS: {mismatched}"
