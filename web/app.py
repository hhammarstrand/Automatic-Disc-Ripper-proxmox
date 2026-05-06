"""Flask web application and REST API for Automatic Disc Ripper for Proxmox.

Provides a dashboard UI and JSON API for monitoring/controlling
the ripping pipeline.
"""

import logging
from pathlib import Path

import psutil
from flask import Flask, render_template, request, jsonify, send_file, abort
from sqlalchemy.exc import SQLAlchemyError

from adr.config import Config
from adr.disc import eject_drive, get_drive_models
from adr.identify import TMDB_SEARCH_URL, TMDB_DETAIL_URL, TMDB_IMAGE_BASE, TMDB_IMAGE_BASE_SMALL
from adr.models import (
    Job, JobStatus, get_session,
    ACTIVE_STATUSES, RIP_PHASE_STATUSES, ENCODE_PHASE_STATUSES, TERMINAL_STATUSES,
)
from adr.utils import get_lan_ip, utcnow, format_duration, normalize_drive, BYTES_PER_MB, extract_tmdb_year, get_bundle_root

logger = logging.getLogger(__name__)

_config: Config | None = None
_pipeline_manager = None
_drive_models: dict[str, str] = {}
_preset_cache: dict | None = None
_preset_cache_time: float = 0.0


def create_app(config: Config, pipeline_manager=None) -> Flask:
    global _config, _pipeline_manager, _drive_models
    _config = config
    _pipeline_manager = pipeline_manager
    try:
        _drive_models = get_drive_models()
    except Exception:
        logger.warning("Could not query drive models at startup", exc_info=True)
        _drive_models = {}
    _bundle = get_bundle_root()
    app = Flask(
        __name__,
        template_folder=str(_bundle / "web" / "templates"),
        static_folder=str(_bundle / "web" / "static"),
    )
    app.jinja_env.filters["duration"] = lambda s: format_duration(s) if s else "–"
    _register_ui_routes(app)
    _register_api_routes(app)

    @app.context_processor
    def inject_globals():
        return {
            "lan_ip": get_lan_ip(),
            "lan_port": _config.web_port if _config else 8080,
        }
    return app


def _register_ui_routes(app: Flask) -> None:

    @app.route("/")
    def index():
        session = get_session()
        try:
            active_jobs = (
                session.query(Job)
                .filter(Job.status.in_(ACTIVE_STATUSES))
                .order_by(Job.started_at.desc()).all()
            )
            recent_jobs = (
                session.query(Job)
                .filter(Job.status.in_(TERMINAL_STATUSES))
                .order_by(Job.completed_at.desc()).limit(10).all()
            )
            drives = []
            disabled_set = set(_config.disabled_drives if _config else [])
            if _pipeline_manager:
                drive_list = getattr(_pipeline_manager, 'all_drives', list(_pipeline_manager.drive_pipelines.keys()))
                for drive_letter in drive_list:
                    if drive_letter in disabled_set:
                        continue
                    rip_job = next(
                        (j for j in active_jobs if j.drive == drive_letter and j.status in RIP_PHASE_STATUSES),
                        None,
                    )
                    enc_job = next(
                        (j for j in active_jobs if j.drive == drive_letter and j.status in ENCODE_PHASE_STATUSES),
                        None,
                    ) if not rip_job else None
                    active = rip_job or enc_job
                    drive_status = "ripping" if rip_job else ("encoding" if enc_job else "idle")
                    drives.append({
                        "letter": drive_letter,
                        "model": _drive_models.get(drive_letter, ""),
                        "label": _config.drive_label(drive_letter),
                        "status": drive_status,
                        "job": active,
                        "disabled": False,
                        "auto_eject": _config.should_eject(drive_letter),
                    })
            return render_template(
                "index.html",
                active_jobs=active_jobs,
                recent_jobs=recent_jobs,
                drives=drives,
                plex_path=_config.plex_path if _config else "",
                encode_queue_size=_pipeline_manager.encode_queue.qsize() if _pipeline_manager else 0,
                watch_folder={
                    "enabled": bool(_config.watch_path),
                    "path": _config.watch_path or None,
                    "output_path": _config.watch_output_path or str(_config.completed_path),
                    "interval": _config.watch_interval,
                } if _config else None,
            )
        finally:
            session.close()

    @app.route("/history")
    def history():
        session = get_session()
        try:
            jobs = session.query(Job).order_by(Job.started_at.desc()).all()
            return render_template("history.html", jobs=jobs, plex_path=_config.plex_path if _config else "")
        finally:
            session.close()

    @app.route("/settings")
    def settings():
        cfg = _config.as_dict()
        hidden_drives = list(_config.disabled_drives) if _config else []
        all_drives = []
        if _pipeline_manager:
            drive_list = getattr(_pipeline_manager, 'all_drives', list(_pipeline_manager.drive_pipelines.keys()))
            for dl in drive_list:
                all_drives.append({
                    "letter": dl,
                    "model": _drive_models.get(dl, ""),
                    "label": _config.drive_label(dl),
                })
        return render_template("settings.html", config=cfg, hidden_drives=hidden_drives, all_drives=all_drives)


def _register_api_routes(app: Flask) -> None:

    @app.route("/api/jobs")
    def api_jobs():
        session = get_session()
        try:
            q = session.query(Job).order_by(Job.started_at.desc())
            status_filter = request.args.get("status")
            if status_filter:
                try:
                    q = q.filter(Job.status == JobStatus(status_filter))
                except ValueError:
                    pass
            limit = request.args.get("limit", type=int)
            if limit:
                q = q.limit(limit)
            return jsonify([j.to_dict() for j in q.all()])
        finally:
            session.close()

    @app.route("/api/jobs/active")
    def api_jobs_active():
        session = get_session()
        try:
            jobs = (
                session.query(Job)
                .filter(Job.status.in_(ACTIVE_STATUSES))
                .order_by(Job.started_at.desc()).all()
            )
            return jsonify([j.to_dict() for j in jobs])
        finally:
            session.close()

    @app.route("/api/jobs/<int:job_id>")
    def api_job_detail(job_id: int):
        session = get_session()
        try:
            job = session.get(Job, job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            return jsonify(job.to_dict())
        finally:
            session.close()

    @app.route("/api/jobs/<int:job_id>/cancel", methods=["POST"])
    def api_cancel_job(job_id: int):
        from adr.pipeline import process_registry
        session = get_session()
        try:
            job = session.get(Job, job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            if job.status in (JobStatus.DONE, JobStatus.CANCELLED):
                return jsonify({"error": "Job already finished"}), 400
            job.status = JobStatus.CANCELLED
            job.completed_at = utcnow()
            session.commit()
            killed = process_registry.kill(job_id)
            logger.info("Cancel job %s — subprocess killed: %s", job_id, killed)
            return jsonify({"ok": True, "job": job.to_dict()})
        finally:
            session.close()

    @app.route("/api/drives")
    def api_drives():
        if not _pipeline_manager:
            return jsonify([])
        session = get_session()
        try:
            result = []
            for drive_letter in _pipeline_manager.drive_pipelines:
                rip_job = (
                    session.query(Job)
                    .filter(Job.drive == drive_letter, Job.status.in_(RIP_PHASE_STATUSES))
                    .first()
                )
                enc_job = None
                if not rip_job:
                    enc_job = (
                        session.query(Job)
                        .filter(Job.drive == drive_letter, Job.status.in_(ENCODE_PHASE_STATUSES))
                        .first()
                    )
                active_job = rip_job or enc_job
                drive_status = "ripping" if rip_job else ("encoding" if enc_job else "idle")
                result.append({
                    "drive": drive_letter,
                    "status": drive_status,
                    "job": active_job.to_dict() if active_job else None,
                })
            return jsonify(result)
        finally:
            session.close()

    @app.route("/api/drives/<drive_letter>/toggle", methods=["POST"])
    def api_toggle_drive(drive_letter: str):
        data = request.get_json() or {}
        should_disable = data.get("disabled", True)
        disabled_list = list(_config.disabled_drives)
        drive_upper = normalize_drive(drive_letter)
        if should_disable:
            if drive_upper not in disabled_list:
                disabled_list.append(drive_upper)
        else:
            disabled_list = [d for d in disabled_list if d != drive_upper]
        _config.update({"disabled_drives": disabled_list})
        return jsonify({"ok": True, "disabled_drives": disabled_list})

    @app.route("/api/drives/<drive_letter>/eject", methods=["POST"])
    def api_eject_drive(drive_letter: str):
        dl = normalize_drive(drive_letter)
        ok = eject_drive(dl)
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": f"Could not eject {dl}"}), 500

    @app.route("/api/drives/<drive_letter>/label", methods=["POST"])
    def api_set_drive_label(drive_letter: str):
        data = request.get_json() or {}
        label = str(data.get("label", "")).strip()
        dl = normalize_drive(drive_letter)
        labels = dict(_config.drive_labels)
        if label:
            labels[dl] = label
        else:
            labels.pop(dl, None)
        _config.update({"drive_labels": labels})
        return jsonify({"ok": True, "label": label})

    @app.route("/api/drives/<drive_letter>/eject-toggle", methods=["POST"])
    def api_toggle_eject(drive_letter: str):
        data = request.get_json() or {}
        want_eject = data.get("auto_eject", True)
        no_eject_list = list(_config.no_eject_drives)
        drive_upper = normalize_drive(drive_letter)
        if want_eject:
            no_eject_list = [d for d in no_eject_list if d != drive_upper]
        else:
            if drive_upper not in no_eject_list:
                no_eject_list.append(drive_upper)
        _config.update({"no_eject_drives": no_eject_list})
        return jsonify({"ok": True, "auto_eject": drive_upper not in no_eject_list})

    @app.route("/api/history/clear", methods=["POST"])
    def api_clear_history():
        session = get_session()
        try:
            jobs = session.query(Job).filter(Job.status.in_(TERMINAL_STATUSES)).all()
            deleted = len(jobs)
            for job in jobs:
                session.delete(job)
            session.commit()
            return jsonify({"ok": True, "deleted": deleted})
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error("Failed to clear history: %s", exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            session.close()

    @app.route("/api/jobs/<int:job_id>", methods=["DELETE"])
    def api_delete_job(job_id: int):
        session = get_session()
        try:
            job = session.get(Job, job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            if job.status not in TERMINAL_STATUSES:
                return jsonify({"error": "Can only delete finished jobs"}), 400
            session.delete(job)
            session.commit()
            return jsonify({"ok": True})
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error("Failed to delete job %s: %s", job_id, exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            session.close()

    @app.route("/api/jobs/<int:job_id>/files")
    def api_job_files(job_id: int):
        session = get_session()
        try:
            job = session.get(Job, job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            if not job.output_path:
                return jsonify({"files": []})
            out = Path(job.output_path)
            files = []
            if out.is_dir():
                for f in sorted(out.glob("*.mp4")):
                    files.append({"name": f.name, "size_mb": round(f.stat().st_size / BYTES_PER_MB, 1)})
            return jsonify({"files": files, "title": job.display_title})
        finally:
            session.close()

    @app.route("/api/jobs/<int:job_id>/stream/<filename>")
    def api_stream_file(job_id: int, filename: str):
        session = get_session()
        try:
            job = session.get(Job, job_id)
            if not job or not job.output_path:
                abort(404)
            out = Path(job.output_path)
            safe_name = Path(filename).name
            file_path = out / safe_name
            if not file_path.exists() or not file_path.suffix.lower() == ".mp4":
                abort(404)
            if not file_path.resolve().is_relative_to(out.resolve()):
                abort(403)
            return send_file(file_path, mimetype="video/mp4", conditional=True)
        finally:
            session.close()

    @app.route("/api/tmdb/search")
    def api_tmdb_search():
        query = request.args.get("q", "").strip()
        year = request.args.get("year", type=int)
        if not query:
            return jsonify({"error": "Missing query parameter 'q'"}), 400
        import requests as req
        api_key = _config.tmdb_api_key if _config else ""
        if not api_key:
            return jsonify({"error": "No TMDb API key configured"}), 400
        params = {"api_key": api_key, "query": query, "include_adult": "false", "language": "en-US"}
        if year:
            params["year"] = str(year)
        try:
            resp = req.get(TMDB_SEARCH_URL, params=params, timeout=10)
            resp.raise_for_status()
            raw_results = resp.json().get("results", [])
            results = []
            for r in raw_results[:15]:
                rd = r.get("release_date", "")
                poster = r.get("poster_path")
                results.append({
                    "tmdb_id": r.get("id"),
                    "title": r.get("title", ""),
                    "original_title": r.get("original_title", ""),
                    "year": extract_tmdb_year(rd),
                    "overview": (r.get("overview", "") or "")[:200],
                    "poster_url": f"{TMDB_IMAGE_BASE_SMALL}{poster}" if poster else None,
                    "popularity": r.get("popularity", 0),
                })
            return jsonify({"results": results})
        except (req.RequestException, ValueError, KeyError) as exc:
            logger.warning("TMDb search failed: %s", exc)
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/jobs/<int:job_id>/rematch", methods=["POST"])
    def api_rematch_job(job_id: int):
        import requests as req
        data = request.get_json() or {}
        tmdb_id = data.get("tmdb_id")
        if not tmdb_id:
            return jsonify({"error": "Missing tmdb_id"}), 400
        api_key = _config.tmdb_api_key if _config else ""
        if not api_key:
            return jsonify({"error": "No TMDb API key configured"}), 400
        session = get_session()
        try:
            job = session.get(Job, job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            detail_resp = req.get(
                f"{TMDB_DETAIL_URL}/{tmdb_id}",
                params={"api_key": api_key, "language": "en-US"},
                timeout=10,
            )
            detail_resp.raise_for_status()
            movie = detail_resp.json()
            old_title = job.display_title
            job.title = movie.get("title", job.title)
            rd = movie.get("release_date", "")
            job.year = extract_tmdb_year(rd, fallback=job.year)
            job.tmdb_id = tmdb_id
            poster_path = movie.get("poster_path")
            if poster_path:
                job.poster_url = f"{TMDB_IMAGE_BASE}{poster_path}"
            if job.status in TERMINAL_STATUSES:
                from adr.pipeline import rename_job_output
                rename_job_output(job, session)
                logger.info("Re-matched job %s: '%s' -> '%s'", job_id, old_title, job.display_title)
                if _config and _config.plex_path and _config.auto_move_to_plex:
                    job.move_to_plex = True
                    session.commit()
                    from adr.pipeline import move_to_plex
                    move_to_plex(job, session, _config)
            else:
                if _config and _config.plex_path and _config.auto_move_to_plex:
                    job.move_to_plex = True
                logger.info("Re-matched job %s: '%s' -> '%s' (metadata only, rename deferred)", job_id, old_title, job.display_title)
            session.commit()
            return jsonify({"ok": True, "job": job.to_dict()})
        except (req.RequestException, SQLAlchemyError, ValueError, KeyError, OSError) as exc:
            session.rollback()
            logger.warning("Re-match failed for job %s: %s", job_id, exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            session.close()

    @app.route("/api/status")
    def api_status():
        if _pipeline_manager:
            return jsonify(_pipeline_manager.get_status())
        return jsonify({"drives": [], "encode_queue_size": 0, "encoder_workers": 0})

    @app.route("/api/jobs/<int:job_id>/toggle-plex", methods=["POST"])
    def api_toggle_plex(job_id: int):
        data = request.get_json() or {}
        if "move_to_plex" not in data:
            return jsonify({"error": "Missing move_to_plex"}), 400
        session = get_session()
        try:
            job = session.get(Job, job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            job.move_to_plex = bool(data["move_to_plex"])
            session.commit()
            return jsonify({"ok": True, "move_to_plex": job.move_to_plex})
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error("Failed to toggle plex flag for job %s: %s", job_id, exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            session.close()

    @app.route("/api/jobs/<int:job_id>/move-to-plex", methods=["POST"])
    def api_move_to_plex(job_id: int):
        if not _config or not _config.plex_path:
            return jsonify({"error": "Plex path not configured"}), 400
        session = get_session()
        try:
            job = session.get(Job, job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            if job.plex_path:
                return jsonify({"error": "Already moved to Plex"}), 400
            if not job.output_path:
                return jsonify({"error": "No output path"}), 400
            job.move_to_plex = True
            session.commit()
            from adr.pipeline import move_to_plex
            ok = move_to_plex(job, session, _config)
            if ok:
                return jsonify({"ok": True, "plex_path": job.plex_path})
            return jsonify({"error": "Move failed — see log"}), 500
        except (OSError, SQLAlchemyError) as exc:
            session.rollback()
            logger.error("Failed to move job %s to Plex: %s", job_id, exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            session.close()

    @app.route("/api/system")
    def api_system():
        cpu_percent = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        try:
            disk_path = str(_config.completed_path) if _config else "/"
            disk = psutil.disk_usage(disk_path)
            disk_info = {
                "total_gb": round(disk.total / (1024**3), 1),
                "used_gb": round(disk.used / (1024**3), 1),
                "free_gb": round(disk.free / (1024**3), 1),
                "percent": disk.percent,
            }
        except OSError:
            logger.debug("Could not get disk usage info", exc_info=True)
            disk_info = None
        gpu_info = None
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,name",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                parts = [p.strip() for p in result.stdout.strip().split(",")]
                if len(parts) >= 4:
                    gpu_info = {
                        "name": parts[3],
                        "utilization": float(parts[0]),
                        "memory_used_mb": float(parts[1]),
                        "memory_total_mb": float(parts[2]),
                        "memory_percent": round(float(parts[1]) / float(parts[2]) * 100, 1) if float(parts[2]) > 0 else 0,
                    }
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            pass
        except (ValueError, IndexError):
            logger.debug("Failed to parse nvidia-smi output", exc_info=True)
        return jsonify({
            "cpu_percent": cpu_percent,
            "ram": {
                "total_gb": round(mem.total / (1024**3), 1),
                "used_gb": round(mem.used / (1024**3), 1),
                "percent": mem.percent,
            },
            "disk": disk_info,
            "gpu": gpu_info,
        })

    @app.route("/api/presets")
    def api_presets():
        import time as _time
        global _preset_cache, _preset_cache_time
        now = _time.monotonic()
        if _preset_cache is None or (now - _preset_cache_time) > 300:
            from adr.encoder import HandBrakeEncoder
            encoder = HandBrakeEncoder(_config)
            _preset_cache = encoder.list_presets()
            _preset_cache_time = now
        return jsonify(_preset_cache)

    @app.route("/api/preset-check")
    def api_preset_check():
        import json as _json
        preset_file = _config.handbrake_preset_file if _config else ""
        preset_name = _config.handbrake_preset if _config else ""
        info = {
            "preset_name": preset_name,
            "preset_file": preset_file,
            "file_exists": False,
            "valid_json": False,
            "preset_names_in_file": [],
            "name_match": False,
            "error": None,
        }
        if not preset_file:
            info["error"] = "No preset file configured (handbrake_preset_file is empty)"
            return jsonify(info)
        import os
        if not os.path.isfile(preset_file):
            info["error"] = f"File not found: {preset_file}"
            return jsonify(info)
        info["file_exists"] = True
        try:
            with open(preset_file, "r", encoding="utf-8") as fh:
                data = _json.load(fh)
            info["valid_json"] = True
            from adr.encoder import HandBrakeEncoder
            names: list[str] = []
            seen: set[str] = set()
            preset_list = data.get("PresetList", [])
            if isinstance(preset_list, list):
                for entry in preset_list:
                    HandBrakeEncoder._extract_preset_names(entry, names, seen)
            if "PresetName" in data and data["PresetName"] not in seen:
                names.append(data["PresetName"])
            info["preset_names_in_file"] = names
            info["name_match"] = preset_name in names
            if not info["name_match"] and names:
                info["error"] = (
                    f"Preset '{preset_name}' not found in file. "
                    f"Available presets: {', '.join(names)}"
                )
        except _json.JSONDecodeError as exc:
            info["error"] = f"Invalid JSON: {exc}"
        except (OSError, KeyError, TypeError) as exc:
            info["error"] = str(exc)
        return jsonify(info)

    @app.route("/api/settings", methods=["GET"])
    def api_get_settings():
        return jsonify(_config.as_dict())

    @app.route("/api/settings", methods=["POST"])
    def api_save_settings():
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        errors = []
        if "web_port" in data:
            try:
                p = int(data["web_port"])
                if not (1 <= p <= 65535):
                    errors.append("web_port must be 1–65535")
            except (TypeError, ValueError):
                errors.append("web_port must be an integer")
        if "max_encode_jobs" in data:
            try:
                n = int(data["max_encode_jobs"])
                if n < 1:
                    errors.append("max_encode_jobs must be >= 1")
            except (TypeError, ValueError):
                errors.append("max_encode_jobs must be an integer")
        if "watch_interval" in data:
            try:
                v = float(data["watch_interval"])
                if v < 1:
                    errors.append("watch_interval must be >= 1")
            except (TypeError, ValueError):
                errors.append("watch_interval must be a number")
        if "log_level" in data:
            if data["log_level"] not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                errors.append("Invalid log_level")
        if errors:
            return jsonify({"error": "; ".join(errors)}), 400
        _config.update(data)
        _RUNTIME_KEYS = {
            "disabled_drives", "eject_after_rip", "no_eject_drives",
            "tmdb_api_key", "log_level", "plex_path", "auto_move_to_plex",
            "drive_labels",
        }
        needs_restart = bool(set(data.keys()) - _RUNTIME_KEYS)
        return jsonify({"ok": True, "config": _config.as_dict(), "requires_restart": needs_restart})
