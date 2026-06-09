"""MakeMKV CLI wrapper for ripping DVDs/Blu-rays.

Drives makemkvcon64.exe in robot mode, parsing real-time progress
and title information from stdout.
"""

import logging
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from adr.config import Config

logger = logging.getLogger(__name__)

# Robot-mode line prefixes we care about
# MSG:code,flags,count,"message",... → log messages
# PRGV:current,total,max            → overall progress
# PRGT:code,id,"name"               → progress title (what's happening)
# PRGC:code,id,"name"               → progress current item
# TINFO:index,code,ap,"value"       → title info
# CINFO:index,code,ap,"value"       → disc info
# SINFO:tindex,sindex,code,ap,"value" → stream/track info
# DRV:index,vis,enabled,flags,"drive_name","disc_name"


class RipResult:
    """Result of a ripping operation."""

    def __init__(self):
        self.success: bool = False
        self.output_dir: Path | None = None
        self.mkv_files: list[Path] = []
        self.title_info: dict[int, dict] = {}  # index -> {name, chapters, duration, ...}
        self.disc_name: str | None = None
        self.error: str | None = None


class MakeMKVRipper:
    """Wrapper around makemkvcon64.exe for ripping optical media."""

    def __init__(self, config: Config, process_registry=None):
        self._exe = config.makemkv_path
        self._min_length = config.min_title_length
        self._raw_path = config.raw_path
        self._active_proc: subprocess.Popen | None = None
        self._process_registry = process_registry

        if not os.path.isfile(self._exe):
            logger.warning("MakeMKV not found at %s", self._exe)

    @property
    def active_proc(self) -> subprocess.Popen | None:
        """The currently running MakeMKV subprocess, or None."""
        return self._active_proc

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @staticmethod
    def _make_dev_source(drive: str) -> str:
        """Build the MakeMKV device source string for a drive.

        On Linux MakeMKV addresses drives by device node, e.g. `dev:/dev/sr0`.
        This avoids the unreliable disc-index lookup entirely. Legacy Windows
        drive letters (e.g. "G:") are still accepted so the parser tests keep
        exercising both forms.
        """
        d = (drive or "").strip()
        if d.startswith("/dev/"):
            return f"dev:{d}"
        # Legacy Windows drive-letter form.
        d = d.rstrip("\\")
        if not d.endswith(":"):
            d += ":"
        return f"dev:{d}"

    def scan_disc(self, drive_letter: str) -> dict[int, dict]:
        """Scan a disc and return title information without ripping.

        Returns dict mapping title index -> info dict.
        """
        titles: dict[int, dict] = {}
        source = self._make_dev_source(drive_letter)
        cmd = [
            self._exe, "-r", "info",
            source,
            f"--minlength={self._min_length}",
            "--messages=-stdout",
        ]
        logger.info("Scanning disc: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            logger.debug("Scan exit code: %d, stdout lines: %d",
                         result.returncode, len(result.stdout.splitlines()))
            for line in result.stdout.splitlines():
                if line.startswith("TINFO:"):
                    self._parse_tinfo(line, titles)
            if not titles:
                logger.warning("Scan produced no TINFO lines (exit=%d)", result.returncode)
                if result.stderr:
                    logger.debug("Scan stderr: %s", result.stderr[:500])
        except subprocess.TimeoutExpired:
            logger.error("Disc scan timed out after 300s for %s", drive_letter)
        except (subprocess.SubprocessError, OSError) as exc:
            logger.error("Disc scan failed: %s", exc, exc_info=True)
        return titles

    def rip(
        self,
        drive_letter: str,
        job_id: int,
        progress_callback: Callable[[dict], None] | None = None,
        title_index: int | None = None,
    ) -> RipResult:
        """Rip all qualifying titles from a disc.

        Uses MakeMKV's dev: source syntax to address the drive directly
        by its Windows drive letter, avoiding disc-index lookup issues.

        Args:
            drive_letter: e.g. "G:"
            job_id: Used to create unique output subdirectory.
            progress_callback: Called with a dict containing:
                overall (float 0-1), title_progress (float 0-1),
                title_current (int), title_total (int), description (str).

        Returns:
            RipResult with success status, file list, and metadata.
        """
        result = RipResult()
        source = self._make_dev_source(drive_letter)

        # Prepare output directory
        output_dir = self._raw_path / str(job_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        result.output_dir = output_dir

        cmd = [
            self._exe,
            "mkv",
            source,
            str(title_index) if title_index is not None else "all",
            str(output_dir),
            "-r",
            f"--minlength={self._min_length}",
            "--messages=-stdout",
        ]

        # Write progress to a temp file to bypass Windows pipe buffering.
        # MakeMKV's C runtime full-buffers stdout when piped (~4KB),
        # meaning short PRGV lines never reach Python until the buffer
        # fills.  Writing progress to a file avoids pipes entirely —
        # a poll thread reads the file every 0.5s to pick up new lines.
        progress_file = None
        progress_thread = None
        _stop_progress = threading.Event()

        try:
            # delete=False is intentional: MakeMKV writes to this path while a
            # poll thread reads it; we close it immediately and unlink in finally.
            progress_file = tempfile.NamedTemporaryFile(  # noqa: SIM115
                mode="w", suffix="_mkv_progress.txt", delete=False,
            )
            progress_path = progress_file.name
            progress_file.close()  # MakeMKV writes to it; we read
            cmd.append(f"--progress={progress_path}")
        except OSError:
            logger.warning("Could not create progress temp file; progress will be unavailable", exc_info=True)
            progress_path = None

        logger.info("Starting MakeMKV rip: %s (title_index=%s)", " ".join(cmd), title_index)

        # Track which title is currently being copied.
        # MakeMKV's PRGV "total" value can jump to 100% between internal
        # phases, so the UI should only advance while a title copy is active.
        _rip_state = {
            "title_current": 0,
            "title_total": 0,
            "description": "",
            "is_copying_title": False,
            "is_rip_active": False,
        }

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self._active_proc = proc

            # Register with process registry for cancellation support
            if self._process_registry and job_id:
                self._process_registry.register(job_id, proc)

            # Start a background thread that reads the progress file
            _prgv_count_box = [0]

            def _poll_progress_file():
                """Read the progress temp file in a loop, parsing new PRGV/PRGC/PRGT lines."""
                if not progress_path:
                    return
                last_pos = 0
                while not _stop_progress.is_set():
                    _stop_progress.wait(0.5)
                    try:
                        with open(progress_path, encoding="utf-8", errors="replace") as f:
                            f.seek(last_pos)
                            new_data = f.read()
                            if not new_data:
                                continue
                            last_pos = f.tell()
                        for line in new_data.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("PRGV:"):
                                _prgv_count_box[0] += 1
                                if _prgv_count_box[0] <= 3:
                                    logger.info("MakeMKV PRGV #%d: %s", _prgv_count_box[0], line)
                                pinfo = self._parse_progress_rich(line, _rip_state)
                                if pinfo and progress_callback:
                                    progress_callback(pinfo)
                            elif line.startswith("PRGC:"):
                                self._parse_prgc(line, _rip_state)
                            elif line.startswith("PRGT:"):
                                self._parse_prgt(line, _rip_state)
                    except FileNotFoundError:
                        pass
                    except (OSError, UnicodeDecodeError):
                        logger.debug("Error reading progress file", exc_info=True)

            if progress_path:
                progress_thread = threading.Thread(
                    target=_poll_progress_file, daemon=True, name="MKVProgressPoll",
                )
                progress_thread.start()

            # Read stdout for messages, TINFO, CINFO etc.
            # These arrive through the pipe and may be buffered, but that's
            # fine — we only need them when the rip finishes.
            _line_count = 0
            remainder = b""
            while True:
                try:
                    chunk = os.read(proc.stdout.fileno(), 8192)
                except OSError:
                    break
                if not chunk:
                    break
                remainder += chunk
                parts = re.split(b"\\r\\n|\\n|\\r", remainder)
                remainder = parts[-1]  # incomplete line
                for raw_bytes in parts[:-1]:
                    line = raw_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    _line_count += 1
                    if _line_count <= 5:
                        logger.debug("MakeMKV line %d: %s", _line_count, line[:120])

                    if line.startswith("TINFO:"):
                        self._parse_tinfo(line, result.title_info)
                    elif line.startswith("CINFO:"):
                        self._parse_cinfo(line, result)
                    elif line.startswith("MSG:"):
                        self._log_message(line)
                    # Progress lines may also appear here if --progress
                    # was not set; handle them as fallback
                    elif line.startswith("PRGV:"):
                        _prgv_count_box[0] += 1
                        pinfo = self._parse_progress_rich(line, _rip_state)
                        if pinfo and progress_callback:
                            progress_callback(pinfo)
                    elif line.startswith("PRGC:"):
                        self._parse_prgc(line, _rip_state)
                    elif line.startswith("PRGT:"):
                        self._parse_prgt(line, _rip_state)

            proc.wait()
            self._active_proc = None

            # Stop progress polling thread
            _stop_progress.set()
            if progress_thread:
                progress_thread.join(timeout=3)

            logger.info(
                "MakeMKV rip finished: exit=%d, stdout_lines=%d, PRGV_total=%d",
                proc.returncode, _line_count, _prgv_count_box[0],
            )

            # Unregister from process registry
            if self._process_registry and job_id:
                self._process_registry.unregister(job_id, proc)

            if proc.returncode == 0:
                # Collect output MKV files
                result.mkv_files = sorted(output_dir.glob("*.mkv"))
                result.success = len(result.mkv_files) > 0
                if result.success:
                    logger.info(
                        "Rip complete: %d MKV files in %s",
                        len(result.mkv_files), output_dir,
                    )
                else:
                    result.error = "MakeMKV exited OK but produced no MKV files"
                    logger.warning(result.error)
            else:
                result.error = f"MakeMKV exited with code {proc.returncode}"
                logger.error(result.error)

        except FileNotFoundError:
            result.error = f"MakeMKV executable not found: {self._exe}"
            logger.error(result.error)
        except (subprocess.SubprocessError, OSError) as exc:
            result.error = str(exc)
            logger.exception("MakeMKV rip failed")
        finally:
            # Ensure progress polling is stopped
            _stop_progress.set()
            if progress_thread and progress_thread.is_alive():
                progress_thread.join(timeout=3)
            # Clean up temp file
            if progress_path:
                try:
                    os.unlink(progress_path)
                except OSError:
                    logger.debug("Could not remove progress temp file %s", progress_path, exc_info=True)

        return result

    # ------------------------------------------------------------------ #
    # Robot-mode output parsers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_csv_line(text: str) -> list[str]:
        """Parse a MakeMKV robot CSV line, respecting quoted strings."""
        parts: list[str] = []
        current = ""
        in_quotes = False
        for ch in text:
            if ch == '"':
                in_quotes = not in_quotes
                current += ch
            elif ch == "," and not in_quotes:
                parts.append(current)
                current = ""
            else:
                current += ch
        parts.append(current)
        return parts

    @staticmethod
    def _parse_progress_rich(line: str, state: dict) -> dict | None:
        """Parse PRGV:current,total,max into a rich progress dict.

        MakeMKV emits PRGV for several internal phases, and its "total"
        value can briefly hit 100% before the rip is actually finished.
        We therefore derive the dashboard progress only while MakeMKV is
        actively copying a title ("Copying title N of M").
        """
        try:
            parts = line[5:].split(",")
            current = int(parts[0])
            pmax = int(parts[2])
            if pmax <= 0:
                return None

            title_current = int(state.get("title_current", 0) or 0)
            title_total = int(state.get("title_total", 0) or 0)
            title_progress = min(max(current / pmax, 0.0), 1.0)

            if not state.get("is_rip_active"):
                return None

            if title_current <= 0:
                title_current = 1
            if title_total <= 0:
                title_total = max(title_current, 1)
            title_current = min(max(title_current, 1), title_total)
            overall = ((title_current - 1) + title_progress) / title_total

            # Leave room for MakeMKV's final flush/close before marking 100%.
            overall = min(max(overall, 0.0), 0.995)
            return {
                "overall": overall,
                "title_progress": title_progress,
                "title_current": title_current,
                "title_total": title_total,
                "description": state.get("description", ""),
            }
        except (IndexError, ValueError):
            logger.debug("Failed to parse PRGV line: %s", line[:80], exc_info=True)
        return None

    @staticmethod
    def _parse_prgc(line: str, state: dict) -> None:
        """Parse PRGC:code,id,"name" - current item being processed.

        Extracts 'Copying title N of M' to track which title is ripping.
        """
        try:
            parts = MakeMKVRipper._parse_csv_line(line[5:])
            code = int(parts[0]) if parts else 0
            desc = parts[2].strip('"') if len(parts) > 2 else ""
            state["description"] = desc
            m = re.search(r"copying\s+title\s+(\d+)\s+of\s+(\d+)", desc, flags=re.IGNORECASE)
            is_saving = code in (5017, 5024) or "saving to mkv file" in desc.lower()
            state["is_copying_title"] = bool(m)
            state["is_rip_active"] = bool(m) or is_saving
            if m:
                state["title_current"] = int(m.group(1))
                state["title_total"] = int(m.group(2))
            elif is_saving and not state.get("title_total"):
                state["title_current"] = 1
                state["title_total"] = 1
        except (IndexError, ValueError):
            logger.debug("Failed to parse PRGC line: %s", line[:80], exc_info=True)

    @staticmethod
    def _parse_prgt(line: str, state: dict) -> None:
        """Parse PRGT:code,id,"name" - high-level phase description."""
        try:
            parts = MakeMKVRipper._parse_csv_line(line[5:])
            code = int(parts[0]) if parts else 0
            desc = parts[2].strip('"') if len(parts) > 2 else ""
            if desc:
                state["description"] = desc
            if code == 5024 or "saving to mkv file" in desc.lower():
                state["is_rip_active"] = True
                if not state.get("title_current"):
                    state["title_current"] = 1
            # Extract total title count from phase like "Saving 3 titles..."
            m = re.search(r"(\d+)\s+title", desc)
            if m and state.get("title_total", 0) == 0:
                state["title_total"] = int(m.group(1))
            elif state.get("is_rip_active") and state.get("title_total", 0) == 0:
                state["title_total"] = max(int(state.get("title_current", 0) or 0), 1)
        except (IndexError, ValueError):
            logger.debug("Failed to parse PRGT line: %s", line[:80], exc_info=True)

    @staticmethod
    def _parse_tinfo(line: str, titles: dict[int, dict]) -> None:
        """Parse TINFO:index,code,ap,"value" into titles dict."""
        try:
            parts = MakeMKVRipper._parse_csv_line(line[6:])
            idx = int(parts[0])
            code = int(parts[1])
            value = parts[3].strip('"')

            if idx not in titles:
                titles[idx] = {}

            # Interesting codes:
            # 2 = name, 8 = chapter count, 9 = duration (H:MM:SS),
            # 10 = size (bytes), 11 = size (human), 27 = filename
            code_map = {2: "name", 8: "chapters", 9: "duration", 10: "size_bytes", 11: "size", 27: "filename"}
            if code in code_map:
                titles[idx][code_map[code]] = value
        except (IndexError, ValueError):
            logger.debug("Failed to parse TINFO line: %s", line[:80], exc_info=True)

    @staticmethod
    def _parse_cinfo(line: str, result: RipResult) -> None:
        """Parse CINFO:index,code,ap,"value" for disc-level info."""
        try:
            parts = MakeMKVRipper._parse_csv_line(line[6:])
            code = int(parts[1])
            value = parts[3].strip('"')
            # Code 2 = disc name
            if code == 2:
                result.disc_name = value
        except (IndexError, ValueError):
            logger.debug("Failed to parse CINFO line: %s", line[:80], exc_info=True)

    @staticmethod
    def _log_message(line: str) -> None:
        """Log a MSG: line from MakeMKV."""
        try:
            parts = MakeMKVRipper._parse_csv_line(line[4:])
            # parts[3] is typically the human-readable message
            if len(parts) > 3:
                msg_text = parts[3].strip('"')
                # MakeMKV error codes >= 2000 are errors, 3000+ are warnings
                code = int(parts[0])
                if code >= 2000:
                    logger.warning("MakeMKV: %s", msg_text)
                else:
                    logger.debug("MakeMKV: %s", msg_text)
        except (IndexError, ValueError):
            logger.debug("MakeMKV raw: %s", line[:120])
