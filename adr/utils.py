"""Utility helpers for Automatic Disc Ripper."""

import logging
import re
import socket
import sys
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)


def get_project_root() -> Path:
    """Return the project root directory.

    Works both in normal Python execution and when bundled with PyInstaller.
    When frozen, bundled data files live in sys._MEIPASS but user files
    (config, database) should live next to the .exe.
    """
    if getattr(sys, "frozen", False):
        # Running as PyInstaller bundle — exe directory
        return Path(sys.executable).resolve().parent
    # Normal Python execution — two levels up from this file (adr/ -> root)
    return Path(__file__).resolve().parent.parent


def get_bundle_root() -> Path:
    """Return the root for bundled read-only data (templates, presets, static).

    When frozen, PyInstaller extracts data files to a temp dir (sys._MEIPASS).
    In normal execution this is the same as the project root.
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging for the application."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def utcnow():
    """Return current local time as a naive datetime (SQLite-compatible).

    Uses the machine's local timezone so times displayed in the web UI
    match the wall clock without any conversion.
    """
    from datetime import datetime
    return datetime.now()


def sanitize_filename(name: str) -> str:
    """Convert a string into a safe filename.

    Removes characters that are illegal in Windows filenames and
    collapses whitespace.  Preserves Unicode letters (å, ä, ö, etc.).
    """
    # Normalise unicode (NFC keeps composed characters like ä intact)
    name = unicodedata.normalize("NFC", name)
    # Remove Windows-illegal characters and control codes
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    # Limit length to stay safely below typical filesystem limits.
    if len(name) > 200:
        name = name[:200]
    return name


def parse_disc_label(label: str) -> tuple[str, int | None]:
    """Parse a disc volume label into a human-readable title and optional year.

    Handles common DVD conventions: underscores, DISC/DISK suffixes,
    region markers, T00/T01 title markers, trailing junk.

    Examples:
        "THE_MATRIX_1999"              -> ("The Matrix", 1999)
        "BEAUTY_AND_THE_BEAST"         -> ("Beauty And The Beast", None)
        "BEAUTY_AND_BEAST_T01"         -> ("Beauty And Beast", None)
        "MY_MOVIE"                     -> ("My Movie", None)
        "Inception_2010_DISC1"         -> ("Inception", 2010)
        "DEADPOOL_2_R1"                -> ("Deadpool 2", None)
    """
    if not label:
        return ("Unknown", None)

    # Work on a copy
    text = label.strip()
    if not text:
        return ("Unknown", None)

    # Remove common DVD suffixes: DISC1, DISK2, D1, D2, etc.
    text = re.sub(r"[_\- ]?(?:DIS[CK]\s*\d+|D\d)$", "", text, flags=re.IGNORECASE)

    # Remove title/track markers: T00, T01, etc.
    text = re.sub(r"[_\- ]?T\d{2,3}$", "", text, flags=re.IGNORECASE)

    # Remove region codes: R1, R2, R4, etc.
    text = re.sub(r"[_\- ]?R\d$", "", text, flags=re.IGNORECASE)

    # Remove PAL/NTSC markers
    text = re.sub(r"[_\- ]?(?:PAL|NTSC)$", "", text, flags=re.IGNORECASE)

    # Try to extract a 4-digit year (1900-2099) near the end
    year = None
    year_match = re.search(r"[_\- ]?((?:19|20)\d{2})[_\- ]?$", text)
    if not year_match:
        # Also try mid-string year
        year_match = re.search(r"[_\- ]((?:19|20)\d{2})[_\- ]", text)
    if year_match:
        year = int(year_match.group(1))
        text = text[: year_match.start()] + text[year_match.end() :]

    # Underscores / hyphens to spaces, title-case
    title = re.sub(r"[_\-]+", " ", text).strip()
    title = re.sub(r"\s+", " ", title)  # collapse multiple spaces
    title = title.title()

    return (title or "Unknown", year)


def format_duration(seconds: int) -> str:
    """Format seconds into 'Xh Ym' or 'Ym Zs'."""
    if seconds < 0:
        return "0s"
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def parse_duration(text: str) -> int:
    """Parse a duration string like 'H:MM:SS' or 'HH:MM:SS' into total seconds."""
    parts = text.strip().split(":")
    if len(parts) == 3:
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            return 0
    return 0


#: Containers a finished job can be in — MP4 from HandBrake, MKV when
#: transcoding is turned off. Kept here rather than imported from adr.naming
#: because adr.naming imports this module.
_FINISHED_SUFFIXES = (".mp4", ".mkv")


def _holds_finished_video(directory: Path) -> bool:
    """True if *directory* already contains a finished file."""
    try:
        return any(
            p.is_file() and p.suffix.lower() in _FINISHED_SUFFIXES
            for p in directory.iterdir()
        )
    except OSError:
        return False


def unique_output_dir(base_dir: Path | str) -> Path:
    """Create base_dir; if it already holds finished video, append (2), (3) etc.

    Prevents output collision when two jobs produce the same Plex title. Both
    containers count: with transcoding off the earlier job left MKVs, and
    looking only for MP4s would write the second film into the first one's
    folder.
    """
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    if not _holds_finished_video(base_dir):
        return base_dir
    for i in range(2, 100):
        candidate = base_dir.parent / f"{base_dir.name} ({i})"
        candidate.mkdir(parents=True, exist_ok=True)
        if not _holds_finished_video(candidate):
            return candidate
    return base_dir


def get_lan_ip() -> str:
    """Return the machine's LAN IP address.

    Uses a UDP connect trick (no traffic sent) to discover which
    interface the OS would use to reach an external address.
    Falls back to 127.0.0.1 if detection fails.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        # Doesn't actually send anything — just causes the OS to pick a source IP
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #

BYTES_PER_MB = 1_048_576


# ------------------------------------------------------------------ #
# Drive helpers
# ------------------------------------------------------------------ #

def normalize_drive(letter: str) -> str:
    """Normalize a drive identifier.

    Linux device paths are returned unchanged (they are case-sensitive):
        "/dev/sr0" -> "/dev/sr0"

    Legacy Windows drive letters are upper-cased without trailing backslash so
    the parsers and tests that still exercise that form keep working:
        "d:\\" -> "D:"
        "d:"   -> "D:"
    """
    s = (letter or "").strip()
    if s.startswith("/dev/"):
        return s
    return s.upper().rstrip("\\")


# ------------------------------------------------------------------ #
# TMDb helpers
# ------------------------------------------------------------------ #

def extract_tmdb_year(release_date: str | None, fallback: int | None = None) -> int | None:
    """Extract a 4-digit year from a TMDb release_date string ('YYYY-MM-DD').

    Returns *fallback* if release_date is missing or too short.
    """
    if release_date and len(release_date) >= 4:
        try:
            return int(release_date[:4])
        except ValueError:
            pass
    return fallback


# ------------------------------------------------------------------ #
# Plex helpers
# ------------------------------------------------------------------ #

def make_plex_folder_name(title: str, year: int | None) -> str:
    """Build a Plex-style folder name: 'Title (Year)' or just 'Title'.

    The title is sanitized for safe use as a filesystem name.
    """
    safe_title = sanitize_filename(title)
    if year:
        return f"{safe_title} ({year})"
    return safe_title
