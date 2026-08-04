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
    # Which program does the transcoding.
    #
    # "handbrake" is the default and the one with the presets. "vaapi" hands
    # the job to ffmpeg and the GPU instead, which exists because a container
    # can have a perfectly working Intel GPU that HandBrake cannot reach: its
    # Quick Sync path goes through the deprecated Intel Media SDK rather than
    # through VA-API, and on current drivers that no longer initialises. The
    # Encoding page probes both and offers the switch when it applies.
    "encoder_backend": "handbrake",
    # The language you want to hear, as a disc spells it: "swe", "eng", "sv".
    # Only the ffmpeg/GPU encoder consults this — HandBrake takes its audio
    # rules from the preset. Empty keeps the disc's own track order, which is
    # right when there is only one language and a coin toss when there are two.
    "audio_language": "",
    # Which VA-API driver HandBrake's Quick Sync should load, via
    # LIBVA_DRIVER_NAME. Empty leaves libva to choose, which is right on a
    # machine with one driver installed and can be wrong on a container that
    # has several: the Media SDK is built against a particular one, and libva
    # picking the other looks exactly like having no GPU at all. The encoder
    # test tries each and fills this in.
    "libva_driver": "",
    # Empty means the first render node found.
    "vaapi_device": "",
    "vaapi_codec": "h264",
    # A quantiser: lower is better quality and a bigger file.
    "vaapi_quality": 22,
    # 0 means "whatever the source is". 1080 halves the size of a 4K rip.
    "vaapi_max_height": 0,
    "max_encode_jobs": 1,
    # Transcoding can be turned off entirely: the MKV MakeMKV produced is kept
    # as it is. Lossless and minutes instead of hours, at several times the
    # size. The watch folder is unaffected — transcoding is the whole reason
    # it exists.
    "transcode_enabled": True,
    "drives": "auto",
    "tmdb_api_key": "",
    "watch_path": "",
    "watch_output_path": "",
    "watch_interval": 5,
    "web_host": "0.0.0.0",
    "web_port": 8080,
    "log_level": "INFO",
    # Where the service log and the per-job logs are written. Empty means a
    # logs/ folder beside the database, on the container's own disk — never
    # the NAS, which would mean a network write per log line.
    "log_path": "",
    "disabled_drives": [],
    "eject_after_rip": True,
    "no_eject_drives": [],
    "main_feature_only": True,
    # Refuse to start a rip unless completed_path is a real mount point.
    # adr-setup-nas turns this on, because with a NAS the difference between
    # "mounted" and "an empty directory on the container disk" is invisible
    # until the disk fills up.
    "require_completed_mount": False,
    # Encode to local disk and transfer the finished file to the destination in
    # one operation, instead of letting HandBrake write across the network for
    # the whole encode. Only takes effect when completed_path is a network
    # filesystem — staging to and from the same local disk would be a pointless
    # extra copy.
    "stage_locally": True,
    "staging_path": "/opt/adr/staging",
    "plex_path": "",
    # Plex keeps films and shows in separate libraries with different naming
    # rules, so a series cannot land in the movie folder. Empty means series
    # go to completed_path like anything else.
    "tv_path": "",
    # What "looks like television" means. A judgement, not a fact: anime runs to
    # 24 minutes, a documentary series to 55, and someone's box set will sit
    # outside any default. Settings rather than constants so a wrong guess is a
    # value to change, not a patch to wait for.
    "series_min_minutes": 15,
    "series_max_minutes": 75,
    "series_min_episodes": 3,
    # Turn detection off entirely and treat every disc as a film.
    "series_detection": True,
    # Series mode: a sticky "every disc is this show" plus an episode counter
    # that advances between discs, so a box set can be fed in without touching
    # the UI. See adr/seriesmode.py.
    "series_mode": False,
    "series_mode_show": "",
    "series_mode_year": None,
    "series_mode_tmdb_id": None,
    "series_mode_season": 1,
    "series_mode_next_episode": 1,
    "series_mode_discs": 0,
    "auto_move_to_plex": True,
    "drive_labels": {},
    # Notifications. The pipeline is meant to be unattended, so "it failed
    # forty minutes ago" has to reach the user rather than wait on the
    # dashboard for them to look.
    "notify_enabled": False,
    "notify_provider": "ntfy",      # ntfy | gotify | discord | webhook
    "notify_url": "",
    "notify_token": "",
    "notify_events": ["job_done", "job_failed"],
    # Cancel a disc that has already been ripped, rather than spending forty
    # minutes on a file that is already in the library. Off by default:
    # re-ripping is legitimate when the first attempt came from a scratched
    # disc or a worse preset, and a false positive that silently skips a disc
    # is a worse outcome than one that warns.
    "skip_duplicates": False,
    # Ask Plex to scan after a film lands, instead of it staying invisible
    # until the next scheduled scan.
    "plex_refresh_enabled": False,
    "plex_url": "",
    "plex_token": "",
    "plex_section": "",
    # Audio CDs and data discs. Both were previously handed to MakeMKV, which
    # fails on them in a way indistinguishable from a broken drive. See
    # adr/disctype.py for how a disc is told apart.
    "audio_cd_enabled": True,
    "audio_cd_format": "flac",          # flac | mp3
    "audio_cd_mp3_bitrate": "320k",
    # Where albums land. Empty means completed_path/Music — music does not
    # belong in a film library, so it never defaults to plex_path.
    "music_path": "",
    "cdparanoia_path": "/usr/bin/cdparanoia",
    "ffmpeg_path": "/usr/bin/ffmpeg",
    # A data disc has nothing to transcode, so the useful thing to do with it
    # is keep a byte-for-byte image. Empty path means completed_path/ISO.
    "data_disc_enabled": True,
    "data_disc_path": "",
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
        for dir_key in ("raw_path", "completed_path", "staging_path"):
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
    def encoder_backend(self) -> str:
        """"handbrake" or "vaapi". Anything unrecognised means HandBrake.

        An unknown value must not stop encoding: a typo in the config file is
        a reason to fall back to the default, not to leave every disc stuck.
        """
        value = str(self._data.get("encoder_backend", "handbrake") or "").lower()
        return value if value in ("handbrake", "vaapi") else "handbrake"

    @property
    def audio_language(self) -> str:
        return self._data.get("audio_language", "") or ""

    @property
    def libva_driver(self) -> str:
        return self._data.get("libva_driver", "") or ""

    @property
    def vaapi_device(self) -> str:
        return self._data.get("vaapi_device", "") or ""

    @property
    def vaapi_codec(self) -> str:
        return self._data.get("vaapi_codec", "h264") or "h264"

    @property
    def vaapi_quality(self) -> int:
        try:
            return int(self._data.get("vaapi_quality", 22))
        except (TypeError, ValueError):
            return 22

    @property
    def vaapi_max_height(self) -> int:
        try:
            return int(self._data.get("vaapi_max_height", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def max_encode_jobs(self) -> int:
        return int(self._data["max_encode_jobs"])

    @property
    def transcode_enabled(self) -> bool:
        """Whether ripped titles are transcoded, or kept as MKV."""
        return bool(self._data.get("transcode_enabled", True))

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

    # -------------------------------------------------------------- #
    # Audio CDs and data discs
    # -------------------------------------------------------------- #

    @property
    def audio_cd_enabled(self) -> bool:
        return bool(self._data.get("audio_cd_enabled", True))

    @property
    def audio_cd_format(self) -> str:
        """Output format for audio CDs: 'flac' or 'mp3'."""
        value = str(self._data.get("audio_cd_format", "flac")).strip().lower()
        return value or "flac"

    @property
    def audio_cd_mp3_bitrate(self) -> str:
        return str(self._data.get("audio_cd_mp3_bitrate", "320k")).strip() or "320k"

    @property
    def music_path(self) -> Path:
        """Where albums land. Defaults to a Music folder beside the films."""
        value = str(self._data.get("music_path", "") or "").strip()
        return Path(value) if value else self.completed_path / "Music"

    @property
    def cdparanoia_path(self) -> str:
        return self._data.get("cdparanoia_path", "/usr/bin/cdparanoia")

    @property
    def ffmpeg_path(self) -> str:
        return self._data.get("ffmpeg_path", "/usr/bin/ffmpeg")

    @property
    def data_disc_enabled(self) -> bool:
        return bool(self._data.get("data_disc_enabled", True))

    @property
    def data_disc_path(self) -> Path:
        """Where disc images land. Defaults to an ISO folder beside the films."""
        value = str(self._data.get("data_disc_path", "") or "").strip()
        return Path(value) if value else self.completed_path / "ISO"

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
    def log_path(self) -> str:
        return self._data.get("log_path", "") or ""

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
    def require_completed_mount(self) -> bool:
        """Whether completed_path must be a mount point before a rip may start."""
        return bool(self._data.get("require_completed_mount", False))

    @property
    def stage_locally(self) -> bool:
        """Whether to encode locally and transfer the finished file in one go."""
        return bool(self._data.get("stage_locally", True))

    @property
    def staging_path(self) -> Path:
        """Local scratch directory used while encoding to network storage."""
        return Path(self._data.get("staging_path", "/opt/adr/staging"))

    @property
    def plex_path(self) -> str:
        """Path to Plex movie library folder (empty = disabled)."""
        return self._data.get("plex_path", "")

    @property
    def tv_path(self) -> str:
        """Plex TV library folder (empty = series go to completed_path)."""
        return self._data.get("tv_path", "")

    @property
    def skip_duplicates(self) -> bool:
        """Whether a disc already in the library is cancelled instead of ripped."""
        return bool(self._data.get("skip_duplicates", False))

    @property
    def series_mode(self) -> bool:
        """Whether every inserted disc is this show's episodes."""
        return bool(self._data.get("series_mode", False))

    @property
    def series_mode_show(self) -> str:
        return str(self._data.get("series_mode_show", "") or "").strip()

    @property
    def series_mode_year(self) -> int | None:
        val = self._data.get("series_mode_year")
        return int(val) if val else None

    @property
    def series_mode_tmdb_id(self) -> int | None:
        val = self._data.get("series_mode_tmdb_id")
        return int(val) if val else None

    @property
    def series_mode_season(self) -> int:
        return int(self._data.get("series_mode_season", 1) or 1)

    @property
    def series_mode_next_episode(self) -> int:
        return int(self._data.get("series_mode_next_episode", 1) or 1)

    @property
    def series_mode_discs(self) -> int:
        return int(self._data.get("series_mode_discs", 0) or 0)

    @property
    def series_detection(self) -> bool:
        """Whether to guess that a disc holds episodes."""
        return bool(self._data.get("series_detection", True))

    @property
    def series_min_minutes(self) -> int:
        return int(self._data.get("series_min_minutes", 15))

    @property
    def series_max_minutes(self) -> int:
        return int(self._data.get("series_max_minutes", 75))

    @property
    def series_min_episodes(self) -> int:
        return int(self._data.get("series_min_episodes", 3))

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

    # ------------------------------------------------------------------ #
    # Notifications
    # ------------------------------------------------------------------ #

    @property
    def notify_enabled(self) -> bool:
        return bool(self._data.get("notify_enabled", False))

    @property
    def notify_provider(self) -> str:
        return str(self._data.get("notify_provider", "ntfy") or "").strip().lower()

    @property
    def notify_url(self) -> str:
        return str(self._data.get("notify_url", "") or "").strip()

    @property
    def notify_token(self) -> str:
        return str(self._data.get("notify_token", "") or "").strip()

    @property
    def notify_events(self) -> list[str]:
        """Which events to send. A malformed value means 'none', not 'all'."""
        val = self._data.get("notify_events", [])
        if isinstance(val, str):
            return [e.strip() for e in val.split(",") if e.strip()]
        return [str(e) for e in val] if isinstance(val, list) else []

    # ------------------------------------------------------------------ #
    # Plex library refresh
    # ------------------------------------------------------------------ #

    @property
    def plex_refresh_enabled(self) -> bool:
        return bool(self._data.get("plex_refresh_enabled", False))

    @property
    def plex_url(self) -> str:
        return str(self._data.get("plex_url", "") or "").strip()

    @property
    def plex_token(self) -> str:
        return str(self._data.get("plex_token", "") or "").strip()

    @property
    def plex_section(self) -> str:
        return str(self._data.get("plex_section", "") or "").strip()

    def should_eject(self, drive_letter: str) -> bool:
        """Check if a specific drive should auto-eject after ripping."""
        if not self.eject_after_rip:
            return False
        return normalize_drive(drive_letter) not in self.no_eject_drives

    def __repr__(self) -> str:
        return f"<Config path={self._path}>"
