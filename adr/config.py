"""Configuration management for Automatic Disc Ripper.

Loads settings from config/adr.yaml, validates them, and provides
a singleton Config object used throughout the application.

This is the Linux/Proxmox build: paths default to /opt/adr and optical
drives are addressed by device path (/dev/sr0) instead of drive letters.
"""

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from adr.utils import get_project_root, normalize_drive

logger = logging.getLogger(__name__)

# Project root: works both as normal Python and PyInstaller bundle.
PROJECT_ROOT = get_project_root()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "adr.yaml"
DATABASE_PATH = PROJECT_ROOT / "adr.db"

# Defaults used if keys are missing from YAML
_DEFAULTS: dict[str, Any] = {
    "makemkv_path": "/usr/bin/makemkvcon",
    "handbrake_path": "/usr/bin/HandBrakeCLI",
    "raw_path": "/opt/adr/raw",
    "completed_path": "/opt/adr/completed",
    "min_title_length": 120,
    "handbrake_preset": "Fast 1080p30",
    "handbrake_preset_file": "",
    "handbrake_extra_args": "",
    "max_encode_jobs": 1,
    "drives": "auto",
    "tmdb_api_key": "",
    "watch_path": "",
    "watch_output_path": "",
    "watch_interval": 5,
    "web_host": "0.0.0.0",
    "web_port": 8080,
    "log_level": "INFO",
    "disabled_drives": [],
    "eject_after_rip": True,
    "no_eject_drives": [],
    "main_feature_only": True,
    "plex_path": "",
    "auto_move_to_plex": True,
    "drive_labels": {},
}


class Config:
    """Application configuration loaded from YAML."""

    def __init__(self, config_path: str | Path | None = None):
        self._path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._data: dict[str, Any] = {}
        self.load()

    # ------------------------------------------------------------------ #
    # Public helpers
    # ------------------------------------------------------------------ #

    def load(self) -> None:
        """(Re)load configuration from disk."""
        if self._path.exists():
            with open(self._path, encoding="utf-8") as fh:
                self._data = yaml.safe_load(fh) or {}
            logger.info("Loaded config from %s", self._path)
        else:
            logger.warning("Config file not found at %s – using defaults", self._path)
            self._data = {}

        # Merge defaults for any missing keys
        for key, default in _DEFAULTS.items():
            self._data.setdefault(key, default)

        # On Linux paths are already forward-slash POSIX paths, so no
        # separator normalisation is needed (the Windows build rewrote
        # "/" to "\\" here).

        # Ensure output directories exist (warn instead of crash if
        # the target drive is not available, e.g. network share offline)
        for dir_key in ("raw_path", "completed_path"):
            try:
                os.makedirs(self._data[dir_key], exist_ok=True)
            except OSError as exc:
                logger.warning("Could not create %s (%s): %s", dir_key, self._data[dir_key], exc)

        # Ensure Plex directory exists (if configured)
        plex_val = self._data.get("plex_path", "")
        if plex_val:
            try:
                os.makedirs(plex_val, exist_ok=True)
            except OSError as exc:
                logger.warning("Could not create plex_path (%s): %s", plex_val, exc)

        # Ensure watch directories exist (if configured)
        for dir_key in ("watch_path", "watch_output_path"):
            val = self._data.get(dir_key, "")
            if val:
                try:
                    os.makedirs(val, exist_ok=True)
                except OSError as exc:
                    logger.warning("Could not create %s (%s): %s", dir_key, val, exc)

    def save(self) -> None:
        """Persist current configuration back to YAML."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as fh:
            yaml.dump(self._data, fh, default_flow_style=False, allow_unicode=True)
        logger.info("Saved config to %s", self._path)

    def update(self, new_values: dict[str, Any]) -> None:
        """Update multiple config values and save."""
        self._data.update(new_values)
        self.save()

    def as_dict(self) -> dict[str, Any]:
        """Return a copy of all settings."""
        return dict(self._data)

    # ------------------------------------------------------------------ #
    # Typed accessors
    # ------------------------------------------------------------------ #

    @property
    def makemkv_path(self) -> str:
        return self._data["makemkv_path"]

    @property
    def handbrake_path(self) -> str:
        return self._data["handbrake_path"]

    @property
    def raw_path(self) -> Path:
        return Path(self._data["raw_path"])

    @property
    def completed_path(self) -> Path:
        return Path(self._data["completed_path"])

    @property
    def min_title_length(self) -> int:
        return int(self._data["min_title_length"])

    @property
    def handbrake_preset(self) -> str:
        return self._data["handbrake_preset"]

    @property
    def handbrake_preset_file(self) -> str:
        return self._data.get("handbrake_preset_file", "")

    @property
    def handbrake_extra_args(self) -> str:
        return self._data["handbrake_extra_args"]

    @property
    def max_encode_jobs(self) -> int:
        return int(self._data["max_encode_jobs"])

    @property
    def drives(self) -> list[str] | str:
        return self._data["drives"]

    @property
    def tmdb_api_key(self) -> str:
        """TMDb API key — prefers ADR_TMDB_API_KEY env var over config file."""
        env_key = os.environ.get("ADR_TMDB_API_KEY", "").strip()
        if env_key:
            return env_key
        return self._data["tmdb_api_key"]

    @property
    def watch_path(self) -> str:
        return self._data["watch_path"]

    @property
    def watch_output_path(self) -> str:
        return self._data["watch_output_path"]

    @property
    def watch_interval(self) -> float:
        return float(self._data["watch_interval"])

    @property
    def web_host(self) -> str:
        return self._data["web_host"]

    @property
    def web_port(self) -> int:
        return int(self._data["web_port"])

    @property
    def log_level(self) -> str:
        return self._data["log_level"]

    @property
    def disabled_drives(self) -> list[str]:
        """Drive letters that are hidden/disabled (not monitored)."""
        val = self._data.get("disabled_drives", [])
        if isinstance(val, list):
            return [normalize_drive(d) for d in val]
        return []

    @property
    def eject_after_rip(self) -> bool:
        """Whether to auto-eject disc after ripping completes (global default)."""
        return bool(self._data.get("eject_after_rip", True))

    @property
    def no_eject_drives(self) -> list[str]:
        """Drive letters where auto-eject is disabled."""
        val = self._data.get("no_eject_drives", [])
        if isinstance(val, list):
            return [normalize_drive(d) for d in val]
        return []

    @property
    def main_feature_only(self) -> bool:
        """Whether to rip only the main (longest) title from each disc."""
        return bool(self._data.get("main_feature_only", True))

    @property
    def plex_path(self) -> str:
        """Path to Plex movie library folder (empty = disabled)."""
        return self._data.get("plex_path", "")

    @property
    def auto_move_to_plex(self) -> bool:
        """Whether matched movies are auto-moved to Plex after encoding."""
        return bool(self._data.get("auto_move_to_plex", True))

    @property
    def drive_labels(self) -> dict[str, str]:
        """Custom labels for drives, e.g. {"D:": "Samsung", "E:": "LG External"}."""
        val = self._data.get("drive_labels", {})
        return val if isinstance(val, dict) else {}

    def drive_label(self, drive_letter: str) -> str:
        """Return the user-set label for a drive, or empty string."""
        return self.drive_labels.get(normalize_drive(drive_letter), "")

    def should_eject(self, drive_letter: str) -> bool:
        """Check if a specific drive should auto-eject after ripping."""
        if not self.eject_after_rip:
            return False
        return normalize_drive(drive_letter) not in self.no_eject_drives

    def __repr__(self) -> str:
        return f"<Config path={self._path}>"
