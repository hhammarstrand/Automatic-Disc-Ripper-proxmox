"""HandBrakeCLI wrapper for transcoding MKV → MP4.

Runs HandBrakeCLI as a subprocess, parses JSON progress output,
and supports queueing multiple encode jobs.
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from adr.config import Config
from adr.utils import BYTES_PER_MB, get_bundle_root

logger = logging.getLogger(__name__)


class EncodeResult:
    """Result of a single encode operation."""

    def __init__(self):
        self.success: bool = False
        self.input_path: Path | None = None
        self.output_path: Path | None = None
        self.error: str | None = None


class HandBrakeEncoder:
    """Wrapper around HandBrakeCLI.exe for video transcoding."""

    def __init__(self, config: Config):
        self._exe = config.handbrake_path
        self._preset = config.handbrake_preset
        self._preset_file = config.handbrake_preset_file
        self._extra_args = config.handbrake_extra_args
        self._completed_path = config.completed_path

        # Auto-discover preset file from presets/ directory if not explicitly set
        if not self._preset_file:
            self._preset_file = self._auto_discover_preset_file()

        self._active_proc: subprocess.Popen | None = None
        self._process_registry = None  # set by pipeline for cancellation

        if not os.path.isfile(self._exe):
            logger.warning("HandBrakeCLI not found at %s", self._exe)
        if self._preset_file and not os.path.isfile(self._preset_file):
            logger.warning("HandBrake preset file not found at %s", self._preset_file)

    @property
    def active_proc(self) -> subprocess.Popen | None:
        """The currently running HandBrake subprocess, or None."""
        return self._active_proc

    def _auto_discover_preset_file(self) -> str:
        """Look for a .json preset file in the presets/ directory.

        If exactly one file matches the configured preset name it is
        preferred; otherwise the first .json file found is used.
        """
        presets_dir = get_bundle_root() / "presets"
        if not presets_dir.is_dir():
            return ""
        json_files = sorted(presets_dir.glob("*.json"))
        if not json_files:
            return ""
        # Prefer a file whose stem matches the configured preset name
        for f in json_files:
            if f.stem == self._preset:
                logger.info("Auto-discovered preset file matching preset name: %s", f)
                return str(f)
        # Fall back to the first json file found
        logger.info("Auto-discovered preset file from presets/: %s", json_files[0])
        return str(json_files[0])

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def encode(
        self,
        input_path: Path | str,
        output_dir: Path | str | None = None,
        output_filename: str | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        job_id: int | None = None,
    ) -> EncodeResult:
        """Transcode a single MKV file to MP4.

        Args:
            input_path: Path to source MKV file.
            output_dir: Destination folder (default: config.completed_path).
            output_filename: Override the output filename (without extension).
            progress_callback: Called with a dict containing:
                progress (float 0-1), eta_seconds (int), fps (float),
                fps_avg (float), pass_num (int), pass_total (int),
                state (str: scanning/working/muxing).

        Returns:
            EncodeResult with output path and success status.
        """
        result = EncodeResult()
        input_path = Path(input_path)
        result.input_path = input_path

        if not input_path.exists():
            result.error = f"Input file not found: {input_path}"
            logger.error(result.error)
            return result

        # Determine output path
        dest_dir = Path(output_dir) if output_dir else self._completed_path
        dest_dir.mkdir(parents=True, exist_ok=True)

        if output_filename:
            out_name = output_filename if output_filename.endswith(".mp4") else f"{output_filename}.mp4"
        else:
            out_name = input_path.stem + ".mp4"

        output_path = dest_dir / out_name
        result.output_path = output_path

        # Build command
        cmd = [
            self._exe,
            "-i", str(input_path),
            "-o", str(output_path),
        ]

        # Import custom preset file if configured
        if self._preset_file and os.path.isfile(self._preset_file):
            cmd.extend(["--preset-import-file", self._preset_file])
            logger.debug("Using custom preset file: %s", self._preset_file)

        cmd.append(f"--preset={self._preset}")
        cmd.append("--json")  # HandBrake 1.10.x emits JSON progress on stdout

        # ISOs need --main-feature to auto-select the movie, not a menu loop
        if input_path.suffix.lower() == ".iso":
            cmd.append("--main-feature")

        # Append extra args if configured
        if self._extra_args:
            cmd.extend(self._extra_args.split())

        logger.info("Starting HandBrake encode: %s -> %s", input_path.name, output_path)
        logger.debug("HandBrake cmd: %s", " ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._active_proc = proc

            # Register with process registry for cancellation
            if self._process_registry and job_id is not None:
                self._process_registry.register(job_id, proc)

            # HandBrake 1.10.x writes JSON progress blocks to stdout.
            # We read the raw fd with os.read() so Windows pipe buffering
            # does not delay updates. stderr is drained separately for logs.
            json_buf: list[str] = []
            brace_depth = 0
            in_progress_block = False
            _enc_line_count = 0
            _enc_progress_count = 0
            _enc_start_time = time.monotonic()
            _warned_no_progress = False

            def _drain_stderr() -> None:
                stderr_line_count = 0
                remainder = b""
                try:
                    while True:
                        chunk = os.read(proc.stderr.fileno(), 4096)
                        if not chunk:
                            break
                        remainder += chunk
                        parts = re.split(b"\r\n|\n|\r", remainder)
                        remainder = parts[-1]
                        for raw_bytes in parts[:-1]:
                            stripped = raw_bytes.decode("utf-8", errors="replace").strip()
                            if not stripped:
                                continue
                            stderr_line_count += 1
                            if stderr_line_count <= 10:
                                logger.debug("HandBrake stderr #%d: %s", stderr_line_count, stripped[:150])
                            elif not stripped.startswith("[h2") and "Warning during" not in stripped:
                                logger.debug("HandBrake: %s", stripped[:200])
                except OSError:
                    pass

            stderr_thread = threading.Thread(target=_drain_stderr, daemon=True, name="HandBrakeStderr")
            stderr_thread.start()

            stdout_remainder = b""
            while True:
                try:
                    chunk = os.read(proc.stdout.fileno(), 8192)
                except OSError:
                    break
                if not chunk:
                    break

                # Watchdog: warn once if no progress callbacks after 30s
                if (not _warned_no_progress
                        and _enc_progress_count == 0
                        and time.monotonic() - _enc_start_time > 30):
                    _warned_no_progress = True
                    logger.warning(
                        "No HandBrake progress callbacks after 30s (stdout lines: %d)",
                        _enc_line_count,
                    )

                stdout_remainder += chunk
                parts = re.split(b"\r\n|\n|\r", stdout_remainder)
                stdout_remainder = parts[-1]
                for raw_bytes in parts[:-1]:
                    stripped = raw_bytes.decode("utf-8", errors="replace").strip()
                    if not stripped:
                        continue

                    _enc_line_count += 1
                    if _enc_line_count <= 10:
                        logger.debug("HandBrake stdout #%d: %s", _enc_line_count, stripped[:150])

                    if stripped.startswith("Progress:") and "{" in stripped:
                        in_progress_block = True
                        json_part = stripped[stripped.index("{"):]
                        json_buf = [json_part]
                        brace_depth = json_part.count("{") - json_part.count("}")
                        if brace_depth <= 0:
                            _enc_progress_count += 1
                            self._handle_progress_json("".join(json_buf), progress_callback)
                            json_buf = []
                            in_progress_block = False
                        continue

                    if not in_progress_block and stripped.startswith("{"):
                        in_progress_block = True
                        json_buf = [stripped]
                        brace_depth = stripped.count("{") - stripped.count("}")
                        if brace_depth <= 0:
                            _enc_progress_count += 1
                            self._handle_progress_json("".join(json_buf), progress_callback)
                            json_buf = []
                            in_progress_block = False
                        continue

                    if in_progress_block:
                        json_buf.append(stripped)
                        brace_depth += stripped.count("{") - stripped.count("}")
                        if brace_depth <= 0:
                            _enc_progress_count += 1
                            self._handle_progress_json(" ".join(json_buf), progress_callback)
                            json_buf = []
                            in_progress_block = False
                        continue

                    if not stripped.startswith("[h2") and "Warning during" not in stripped:
                        logger.debug("HandBrake: %s", stripped[:200])

            proc.wait()
            self._active_proc = None
            stderr_thread.join(timeout=5)

            logger.info(
                "HandBrake finished: exit=%d, stdout_lines=%d, progress_callbacks=%d",
                proc.returncode, _enc_line_count, _enc_progress_count,
            )

            # Unregister from process registry
            if self._process_registry and job_id is not None:
                self._process_registry.unregister(job_id, proc)

            if proc.returncode == 0 and output_path.exists():
                result.success = True
                logger.info("Encode complete: %s (%.1f MB)", output_path, output_path.stat().st_size / BYTES_PER_MB)
            else:
                result.error = f"HandBrake exited with code {proc.returncode}"
                if not output_path.exists():
                    result.error += " (no output file created)"
                logger.error(result.error)

        except FileNotFoundError:
            result.error = f"HandBrakeCLI not found: {self._exe}"
            logger.error(result.error)
        except (subprocess.SubprocessError, OSError) as exc:
            result.error = str(exc)
            logger.exception("HandBrake encode failed")

        return result

    def list_presets(self) -> dict[str, list[str]]:
        """Return built-in HandBrake presets grouped by category.

        Returns dict like {"General": ["Very Fast 1080p30", ...], ...}
        Also includes a 'Custom' category if a preset file is configured.
        """
        categories: dict[str, list[str]] = {}
        current_cat = "Other"

        # ---- Built-in presets from HandBrakeCLI ----
        try:
            result = subprocess.run(
                [self._exe, "--preset-list"],
                capture_output=True, text=True, timeout=30,
            )
            for line in result.stdout.splitlines():
                # Category lines end with / like "    General/"
                stripped = line.rstrip()
                if not stripped:
                    continue
                indent = len(stripped) - len(stripped.lstrip())
                clean = stripped.strip()
                if clean.endswith("/"):
                    current_cat = clean.rstrip("/")
                    continue
                # Preset lines start with "+ " or just the name
                if clean.startswith("+ "):
                    name = clean[2:].strip()
                elif indent >= 4 and not clean.startswith("<") and not clean.startswith("HandBrake"):
                    name = clean
                else:
                    continue
                if name:
                    categories.setdefault(current_cat, []).append(name)
        except FileNotFoundError:
            logger.warning("HandBrakeCLI not found at %s — cannot list presets", self._exe)
        except (subprocess.SubprocessError, OSError) as exc:
            logger.error("Failed to list HandBrake presets: %s", exc, exc_info=True)

        # ---- Custom presets from JSON file(s) in presets/ folder ----
        custom = self._list_custom_presets()
        if custom:
            categories["Custom presets"] = custom

        return categories

    def _list_custom_presets(self) -> list[str]:
        """Read preset names from JSON preset files in presets/ dir and configured file."""
        names: list[str] = []
        seen: set[str] = set()

        # Collect all JSON files to scan
        files_to_scan: list[Path] = []

        # Configured preset file
        if self._preset_file and os.path.isfile(self._preset_file):
            files_to_scan.append(Path(self._preset_file))

        # All .json files in the presets/ directory
        presets_dir = get_bundle_root() / "presets"
        if presets_dir.is_dir():
            for f in sorted(presets_dir.glob("*.json")):
                if f not in files_to_scan:
                    files_to_scan.append(f)

        for json_path in files_to_scan:
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                # HandBrake JSON preset format: {"PresetList": [{"PresetName": ...}, ...]}
                preset_list = data.get("PresetList", [])
                if isinstance(preset_list, list):
                    for entry in preset_list:
                        # Can be nested categories with "ChildrenArray"
                        self._extract_preset_names(entry, names, seen)
                # Also support flat format where top-level has PresetName
                if "PresetName" in data and data["PresetName"] not in seen:
                    names.append(data["PresetName"])
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Could not read preset file %s: %s", json_path, exc)

        return names

    @staticmethod
    def _extract_preset_names(entry: dict, names: list[str], seen: set[str]) -> None:
        """Recursively extract PresetName from a HandBrake preset tree node."""
        if not isinstance(entry, dict):
            return
        name = entry.get("PresetName")
        if name and name not in seen:
            # Skip folder entries (Type=0 means preset, but be lenient)
            if not entry.get("Folder", False):
                names.append(name)
                seen.add(name)
        # Recurse into children
        for child in entry.get("ChildrenArray", []):
            HandBrakeEncoder._extract_preset_names(child, names, seen)

    # ------------------------------------------------------------------ #
    # Progress parsing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_progress_value(value: object) -> float:
        """Normalize HandBrake progress to 0.0-1.0.

        Some HandBrake builds report Progress as a fraction, others as a
        percentage-like value. Accept both to keep the dashboard stable.
        """
        try:
            progress = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if progress > 1.0:
            progress /= 100.0
        return min(max(progress, 0.0), 1.0)

    def _handle_progress_json(self, json_str: str, callback: Callable[[dict], None] | None) -> None:
        """Parse a complete JSON progress block and fire callback with rich info.

        HandBrake may output non-strict JSON (unquoted keys) so we fall
        back to a regex extraction of the Progress value if json.loads fails.
        """
        info: dict | None = None

        # Try strict JSON first
        try:
            data = json.loads(json_str)
            # Normalise State to uppercase string for comparison
            raw_state = data.get("State", data.get("state", ""))
            state = str(raw_state).upper() if raw_state is not None else ""
            if state == "WORKING" or raw_state == 1:
                w = data.get("Working", data.get("working", {})) or {}
                info = {
                    "progress": self._normalize_progress_value(w.get("Progress", w.get("progress", 0))),
                    "eta_seconds": int(w.get("ETASeconds", w.get("eta_seconds", 0)) or 0),
                    "fps": float(w.get("Rate", w.get("rate", 0)) or 0.0),
                    "fps_avg": float(w.get("RateAvg", w.get("rate_avg", 0)) or 0.0),
                    "pass_num": int(w.get("Pass", w.get("pass", 0)) or 0),
                    "pass_total": int(w.get("PassCount", w.get("pass_count", 1)) or 1),
                    "state": "working",
                }
            elif state == "MUXING" or raw_state == 2:
                m_data = data.get("Muxing", data.get("muxing", {})) or {}
                info = {
                    "progress": self._normalize_progress_value(m_data.get("Progress", m_data.get("progress", 0))),
                    "eta_seconds": 0,
                    "fps": 0.0,
                    "fps_avg": 0.0,
                    "pass_num": 0,
                    "pass_total": 1,
                    "state": "muxing",
                }
            elif state == "SCANNING" or raw_state == 3:
                s = data.get("Scanning", data.get("scanning", {})) or {}
                info = {
                    "progress": self._normalize_progress_value(s.get("Progress", s.get("progress", 0))),
                    "eta_seconds": 0,
                    "fps": 0.0,
                    "fps_avg": 0.0,
                    "pass_num": 0,
                    "pass_total": 1,
                    "state": "scanning",
                    "scan_title": int(s.get("Title", s.get("title", 0)) or 0),
                    "scan_title_count": int(s.get("TitleCount", s.get("title_count", 0)) or 0),
                    "scan_preview": int(s.get("Preview", s.get("preview", 0)) or 0),
                    "scan_preview_count": int(s.get("PreviewCount", s.get("preview_count", 0)) or 0),
                }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            # Fallback: regex to find "Progress": 0.25 (with or without quotes on key)
            m = re.search(r'"?Progress"?\s*:\s*([\d.]+)', json_str)
            state_m = re.search(r'"?State"?\s*:\s*"?(\w+)"?', json_str)
            if m and state_m:
                state_str = state_m.group(1).upper()
                if state_str in ("WORKING", "MUXING", "SCANNING"):
                    try:
                        prog_val = self._normalize_progress_value(m.group(1))
                    except ValueError:
                        prog_val = 0.0
                    # Try to extract Rate from regex fallback too
                    fps_val = 0.0
                    rate_m = re.search(r'"?Rate"?\s*:\s*([\d.]+)', json_str)
                    if rate_m:
                        try:
                            fps_val = float(rate_m.group(1))
                        except ValueError:
                            pass
                    info = {
                        "progress": prog_val,
                        "eta_seconds": 0,
                        "fps": fps_val,
                        "fps_avg": 0.0,
                        "pass_num": 0,
                        "pass_total": 1,
                        "state": state_str.lower(),
                    }

        if info is not None and callback:
            callback(info)
