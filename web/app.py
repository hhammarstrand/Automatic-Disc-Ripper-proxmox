"""Flask web application and REST API for Automatic Disc Ripper.

Provides a dashboard UI and JSON API for monitoring/controlling
the ripping pipeline.
"""

import logging
import os
from pathlib import Path

import psutil
from flask import Flask, abort, jsonify, render_template, request, send_file
from sqlalchemy.exc import SQLAlchemyError

from adr import joblog
from adr.config import Config
from adr.disc import eject_drive, get_drive_models
from adr.identify import TMDB_DETAIL_URL, TMDB_IMAGE_BASE, TMDB_IMAGE_BASE_SMALL, TMDB_SEARCH_URL
from adr.models import (
    ACTIVE_STATUSES,
    ENCODE_PHASE_STATUSES,
    RIP_PHASE_STATUSES,
    TERMINAL_STATUSES,
    Job,
    JobStatus,
    get_session,
)
from adr.utils import (
    BYTES_PER_MB,
    extract_tmdb_year,
    format_duration,
    get_bundle_root,
    get_lan_ip,
    normalize_drive,
    utcnow,
)

logger = logging.getLogger(__name__)

# These are set by create_app() so routes can access them
_config: Config | None = None
_pipeline_manager = None  # adr.pipeline.PipelineManager
_drive_models: dict[str, str] = {}
_preset_cache: dict | None = None
_preset_cache_time: float = 0.0

#: Rows per page of history. Large enough that most people never see a second
#: page, small enough that a machine which has worked through a whole shelf
#: still renders instantly.
HISTORY_PAGE_SIZE = 100


def create_app(config: Config, pipeline_manager=None) -> Flask:
    """Create and configure the Flask application.

    Args:
        config: Application configuration.
        pipeline_manager: Optional PipelineManager instance for live status.
    """
    global _config, _pipeline_manager, _drive_models
    _config = config
    _pipeline_manager = pipeline_manager

    # Cache drive model names at startup
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
    # Template filter for time formatting
    app.jinja_env.filters["duration"] = lambda s: format_duration(s) if s else "–"

    # Register routes
    _register_ui_routes(app)
    _register_api_routes(app)

    # Make LAN IP available in all templates
    @app.context_processor
    def inject_globals():
        from adr import __version__, seriesmode

        return {
            "lan_ip": get_lan_ip(),
            "lan_port": _config.web_port if _config else 8080,
            "adr_version": __version__,
            # A mode that silently renames every disc must be visible from
            # wherever the user happens to be looking, not only where it was
            # turned on.
            "series_mode": seriesmode.state(_config) if _config else {"active": False},
        }

    return app


# ------------------------------------------------------------------ #
# UI Routes (HTML pages)
# ------------------------------------------------------------------ #

def _register_ui_routes(app: Flask) -> None:

    @app.route("/")
    def index():
        """Dashboard: active jobs, drive status."""
        session = get_session()
        try:
            active_jobs = (
                session.query(Job)
                .filter(Job.status.in_(ACTIVE_STATUSES))
                .order_by(Job.started_at.desc())
                .all()
            )
            # For drive cards, only rip-phase jobs count as "drive busy".
            # Encoding runs in a background pool — the drive is idle.
            recent_jobs = (
                session.query(Job)
                .filter(Job.status.in_(TERMINAL_STATUSES))
                .order_by(Job.completed_at.desc())
                .limit(10)
                .all()
            )

            # Drive info — only show active (non-disabled) drives on dashboard
            drives = []
            disabled_set = set(d.upper() for d in (_config.disabled_drives if _config else []))
            if _pipeline_manager:
                drive_list = getattr(_pipeline_manager, 'all_drives', list(_pipeline_manager.drive_pipelines.keys()))
                for drive_letter in drive_list:
                    is_disabled = drive_letter.upper() in disabled_set
                    if is_disabled:
                        continue  # completely hidden on dashboard
                    rip_job = next(
                        (j for j in active_jobs if j.drive == drive_letter and j.status in RIP_PHASE_STATUSES),
                        None,
                    )
                    # Also expose the encoding job so the card can show progress
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
        """Job history, a page at a time.

        This used to fetch every job ever run and let the template ask each one
        for its tracks — one query per row. A machine that has worked through a
        shelf of discs has thousands of rows, and the page got slower every
        week. Filtering moved to the server for the same reason: filtering in
        the browser can only hide rows that were already sent.
        """
        from sqlalchemy.orm import selectinload

        status = (request.args.get("status") or "").strip().lower()
        try:
            page = max(1, int(request.args.get("page", 1)))
        except (TypeError, ValueError):
            page = 1

        session = get_session()
        try:
            query = session.query(Job)
            if status:
                try:
                    query = query.filter(Job.status == JobStatus(status))
                except ValueError:
                    status = ""          # an unknown status filters nothing
            total = query.count()
            pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
            page = min(page, pages)
            jobs = (
                query.options(selectinload(Job.tracks))
                .order_by(Job.started_at.desc())
                .limit(HISTORY_PAGE_SIZE)
                .offset((page - 1) * HISTORY_PAGE_SIZE)
                .all()
            )
            return render_template(
                "history.html", jobs=jobs,
                plex_path=_config.plex_path if _config else "",
                page=page, pages=pages, total=total, status=status,
                page_size=HISTORY_PAGE_SIZE,
            )
        finally:
            session.close()

    @app.route("/settings")
    def settings():
        """Settings page."""
        cfg = _config.as_dict()
        # Also pass the disabled drives list for the hidden-drives management UI
        hidden_drives = list(_config.disabled_drives) if _config else []
        # Build drive info list for label management
        all_drives = []
        if _pipeline_manager:
            drive_list = getattr(_pipeline_manager, 'all_drives', list(_pipeline_manager.drive_pipelines.keys()))
            for dl in drive_list:
                all_drives.append({
                    "letter": dl,
                    "model": _drive_models.get(dl, ""),
                    "label": _config.drive_label(dl),
                })
        # music_path and data_disc_path are stored empty and computed from
        # completed_path. The form needs both: the stored value to edit, and
        # the computed one as the placeholder saying where things go today.
        defaults = {
            "music_path": str(_config.music_path),
            "data_disc_path": str(_config.data_disc_path),
        }
        return render_template("settings.html", config=cfg, hidden_drives=hidden_drives,
                               all_drives=all_drives, defaults=defaults)

    @app.route("/storage")
    def storage_page():
        """Storage page: where files actually land, and how to attach a NAS."""
        return render_template(
            "storage.html",
            ctid=os.environ.get("ADR_CTID", "").strip() or "",
        )

    @app.route("/doctor")
    def doctor_page():
        """Doctor page: self-checks, the host-side command, and updates."""
        return render_template(
            "doctor.html",
            ctid=os.environ.get("ADR_CTID", "").strip() or "",
        )


# ------------------------------------------------------------------ #
# REST API Routes
# ------------------------------------------------------------------ #

def _register_api_routes(app: Flask) -> None:

    @app.route("/api/jobs")
    def api_jobs():
        """List all jobs, optionally filtered by status."""
        session = get_session()
        try:
            q = session.query(Job).order_by(Job.started_at.desc())
            status_filter = request.args.get("status")
            if status_filter:
                try:
                    st = JobStatus(status_filter)
                    q = q.filter(Job.status == st)
                except ValueError:
                    pass
            limit = request.args.get("limit", type=int)
            if limit:
                q = q.limit(limit)
            jobs = q.all()
            return jsonify([j.to_dict() for j in jobs])
        finally:
            session.close()

    @app.route("/api/jobs/active")
    def api_jobs_active():
        """Return only active (in-progress) jobs — no limit, server-side filter."""
        session = get_session()
        try:
            jobs = (
                session.query(Job)
                .filter(Job.status.in_(ACTIVE_STATUSES))
                .order_by(Job.started_at.desc())
                .all()
            )
            return jsonify([j.to_dict() for j in jobs])
        finally:
            session.close()

    @app.route("/api/jobs/<int:job_id>")
    def api_job_detail(job_id: int):
        """Get details for a single job."""
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
        """Cancel a job and kill any running subprocess."""
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

            # Kill the running MakeMKV / HandBrake subprocess
            killed = process_registry.kill(job_id)
            logger.info("Cancel job %s — subprocess killed: %s", job_id, killed)

            return jsonify({"ok": True, "job": job.to_dict()})
        finally:
            session.close()

    @app.route("/api/drives")
    def api_drives():
        """Status of all monitored drives."""
        if not _pipeline_manager:
            return jsonify([])
        session = get_session()
        try:
            result = []
            for drive_letter in _pipeline_manager.drive_pipelines:
                # Drive is "busy" only during rip phase
                rip_job = (
                    session.query(Job)
                    .filter(
                        Job.drive == drive_letter,
                        Job.status.in_(RIP_PHASE_STATUSES),
                    )
                    .first()
                )
                enc_job = None
                if not rip_job:
                    enc_job = (
                        session.query(Job)
                        .filter(
                            Job.drive == drive_letter,
                            Job.status.in_(ENCODE_PHASE_STATUSES),
                        )
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

    # ---------------------------------------------------------------- #
    # Per-drive actions
    #
    # The drive goes in the request body, never in the URL path. A Linux
    # optical drive is identified by a device path, and putting "/dev/sr0" in
    # a URL means percent-encoding its slashes — which Werkzeug then either
    # 308-redirects to a path with the leading slash gone, or refuses to match
    # at all. Both happened here: Rip reported "DEV/SR0 is not a drive this
    # instance watches", and eject-toggle and hide-drive returned 404.
    #
    # The old URL-path routes are kept below so a page cached in a browser
    # from before this change still works.
    # ---------------------------------------------------------------- #

    def _requested_device() -> str:
        """The device named in the request body, normalised."""
        data = request.get_json(silent=True) or {}
        return normalize_drive(str(data.get("device", "")).strip())

    def _toggle_drive(drive_letter: str):
        """Enable or disable a drive. Body: {"disabled": true/false}"""
        data = request.get_json(silent=True) or {}
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

    def _eject(drive_letter: str):
        """Eject the disc tray for a specific drive."""
        dl = normalize_drive(drive_letter)
        ok = eject_drive(dl)
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": f"Could not eject {dl}"}), 500

    def _set_label(drive_letter: str):
        """Set a custom label for a drive. Body: {"label": "My Drive"}"""
        data = request.get_json(silent=True) or {}
        label = str(data.get("label", "")).strip()
        dl = normalize_drive(drive_letter)
        labels = dict(_config.drive_labels)
        if label:
            labels[dl] = label
        else:
            labels.pop(dl, None)
        _config.update({"drive_labels": labels})
        return jsonify({"ok": True, "label": label})

    def _toggle_eject(drive_letter: str):
        """Toggle auto-eject for a specific drive. Body: {"auto_eject": true/false}"""
        data = request.get_json(silent=True) or {}
        want_eject = data.get("auto_eject", True)

        no_eject_list = list(_config.no_eject_drives)
        drive_upper = normalize_drive(drive_letter)

        if want_eject:
            no_eject_list = [d for d in no_eject_list if d != drive_upper]
        elif drive_upper not in no_eject_list:
            no_eject_list.append(drive_upper)

        _config.update({"no_eject_drives": no_eject_list})
        return jsonify({"ok": True, "auto_eject": drive_upper not in no_eject_list})

    def _rip(drive_letter: str):
        """Rip the disc already in this drive.

        The watcher fires on insertion only, so after a failed job the disc
        sits there and nothing restarts it. This is the "try that again" the
        drive card was missing.
        """
        if not _pipeline_manager:
            return jsonify({"ok": False, "message": "The pipeline is not running."}), 503
        ok, message = _pipeline_manager.rip_now(normalize_drive(drive_letter))
        return jsonify({"ok": ok, "message": message}), (200 if ok else 409)

    # The routes the UI uses: device in the body.
    app.add_url_rule("/api/drives/toggle", "api_toggle_drive_body",
                     lambda: _toggle_drive(_requested_device()), methods=["POST"])
    app.add_url_rule("/api/drives/eject", "api_eject_drive_body",
                     lambda: _eject(_requested_device()), methods=["POST"])
    app.add_url_rule("/api/drives/label", "api_set_drive_label_body",
                     lambda: _set_label(_requested_device()), methods=["POST"])
    app.add_url_rule("/api/drives/eject-toggle", "api_toggle_eject_body",
                     lambda: _toggle_eject(_requested_device()), methods=["POST"])
    app.add_url_rule("/api/drives/rip", "api_drive_rip_body",
                     lambda: _rip(_requested_device()), methods=["POST"])

    # The old routes, kept so a browser still holding the previous page works.
    app.add_url_rule("/api/drives/<path:drive_letter>/toggle", "api_toggle_drive",
                     _toggle_drive, methods=["POST"])
    app.add_url_rule("/api/drives/<path:drive_letter>/eject", "api_eject_drive",
                     _eject, methods=["POST"])
    app.add_url_rule("/api/drives/<path:drive_letter>/label", "api_set_drive_label",
                     _set_label, methods=["POST"])
    app.add_url_rule("/api/drives/<path:drive_letter>/eject-toggle", "api_toggle_eject",
                     _toggle_eject, methods=["POST"])
    app.add_url_rule("/api/drives/<path:drive_letter>/rip", "api_drive_rip",
                     _rip, methods=["POST"])

    @app.route("/api/history/clear", methods=["POST"])
    def api_clear_history():
        """Delete all completed/error/cancelled jobs from history."""
        session = get_session()
        try:
            jobs = session.query(Job).filter(
                Job.status.in_(TERMINAL_STATUSES)
            ).all()
            deleted = len(jobs)
            for job in jobs:
                session.delete(job)  # ORM cascade deletes related tracks
            session.commit()
            # Sweep the logs of everything that just went, plus anything left
            # over from an earlier delete that predates job logs.
            remaining = {row[0] for row in session.query(Job.id).all()}
            joblog.prune(_config, keep_job_ids=remaining)
            return jsonify({"ok": True, "deleted": deleted})
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error("Failed to clear history: %s", exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            session.close()

    @app.route("/api/jobs/<int:job_id>", methods=["DELETE"])
    def api_delete_job(job_id: int):
        """Delete a single finished job and its tracks."""
        session = get_session()
        try:
            job = session.get(Job, job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            if job.status not in TERMINAL_STATUSES:
                return jsonify({"error": "Can only delete finished jobs"}), 400
            session.delete(job)
            session.commit()
            # The log belongs to the job; leaving it behind would accumulate
            # files for jobs that no longer exist.
            joblog.delete(_config, job_id)
            return jsonify({"ok": True})
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error("Failed to delete job %s: %s", job_id, exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            session.close()

    @app.route("/api/jobs/<int:job_id>/content-type", methods=["POST"])
    def api_job_content_type(job_id: int):
        """Mark a job as a film or a series, with the season and first episode.

        Only meaningful before the tracks are queued for encoding; afterwards
        the filenames are already decided. Rejected rather than silently
        ignored in that case, because a setting that appears to take and does
        nothing is worse than an error.
        """
        from adr.series import episode_numbers

        data = request.get_json() or {}
        content_type = str(data.get("content_type", "")).strip().lower()
        if content_type not in ("movie", "series"):
            return jsonify({"error": "content_type must be 'movie' or 'series'"}), 400

        session = get_session()
        try:
            job = session.get(Job, job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            if job.status in (JobStatus.ENCODING, JobStatus.DONE):
                return jsonify({
                    "error": "Encoding has already started, so the filenames are set. "
                             "Cancel and retry the job to change this.",
                }), 409

            job.content_type = content_type
            if content_type == "series":
                try:
                    job.series_season = max(0, int(data.get("season", 1)))
                    job.series_first_episode = max(1, int(data.get("first_episode", 1)))
                except (TypeError, ValueError):
                    return jsonify({"error": "season and first_episode must be numbers"}), 400

                # The show, when the user picked one from the TV search. The
                # title on the job came from TMDb's *movie* search, which for a
                # box set returns a confident-looking film — so without this the
                # season is named after whatever the disc label resembled.
                show = str(data.get("show", "")).strip()
                if show:
                    job.title = show
                    year = data.get("year")
                    job.year = int(year) if str(year or "").isdigit() else None
                    tmdb_id = data.get("tmdb_id")
                    if str(tmdb_id or "").isdigit():
                        job.tmdb_id = int(tmdb_id)
                        # The poster is a film's; it no longer describes this job.
                        job.poster_url = None
            else:
                job.series_season = None
                job.series_first_episode = None
            session.commit()

            preview = []
            if content_type == "series":
                from adr.naming import plan_output
                count = max(1, len(job.tracks))
                preview = plan_output(job, count).filenames
                episode_numbers(count, job.series_first_episode or 1)

            return jsonify({
                "ok": True,
                "content_type": job.content_type,
                "season": job.series_season,
                "first_episode": job.series_first_episode,
                "preview": preview,
            })
        except SQLAlchemyError as exc:
            session.rollback()
            return jsonify({"error": str(exc)}), 500
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Series mode
    # ------------------------------------------------------------------ #

    @app.route("/api/series-mode", methods=["GET", "POST"])
    def api_series_mode():
        """Read or change the sticky "every disc is this show" mode.

        POST with ``active: false`` turns it off; with ``active: true`` it
        needs a show name, and takes the season and the episode the *next*
        disc starts at.
        """
        from adr import seriesmode

        if request.method == "GET":
            return jsonify(seriesmode.state(_config))

        data = request.get_json() or {}
        if not data.get("active"):
            return jsonify(seriesmode.stop(_config))

        try:
            result = seriesmode.start(
                _config,
                show=str(data.get("show", "")),
                season=int(data.get("season", 1)),
                first_episode=int(data.get("first_episode", 1)),
                year=int(data["year"]) if str(data.get("year") or "").isdigit() else None,
                tmdb_id=int(data["tmdb_id"]) if str(data.get("tmdb_id") or "").isdigit() else None,
            )
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result)

    @app.route("/api/series-mode/next-episode", methods=["POST"])
    def api_series_mode_next_episode():
        """Correct the counter, for a disc that held bonus material.

        The count comes from how many titles were ripped, which is right until
        a disc includes a feature-length extra that looked like an episode.
        """
        from adr import seriesmode

        data = request.get_json() or {}
        try:
            episode = int(data.get("episode"))
        except (TypeError, ValueError):
            return jsonify({"error": "episode must be a number"}), 400
        return jsonify(seriesmode.set_next_episode(_config, episode))

    @app.route("/api/tmdb/search-tv")
    def api_tmdb_search_tv():
        """Search TMDb for a show, so the user picks rather than the app guessing.

        Naming a whole season from the wrong show is a worse outcome than one
        wrong film, so there is no auto-accept here.
        """
        from adr.identify import search_series

        query = request.args.get("query", "").strip()
        if not query:
            return jsonify({"error": "query is required"}), 400
        if not _config.tmdb_api_key:
            return jsonify({"error": "No TMDb API key configured."}), 400
        return jsonify({"results": search_series(query, _config.tmdb_api_key)})

    @app.route("/api/tmdb/season")
    def api_tmdb_season():
        """Episode titles for one season of a show.

        Correct numbering is all Plex needs, but real titles are how an
        off-by-one gets caught by eye — before forty minutes of encoding, not
        after. Best-effort: a failure here degrades to plain numbers.
        """
        from adr.identify import get_season_episodes

        try:
            tmdb_id = int(request.args.get("tmdb_id", ""))
            season = int(request.args.get("season", ""))
        except ValueError:
            return jsonify({"error": "tmdb_id and season must be numbers"}), 400
        if not _config.tmdb_api_key:
            return jsonify({"episodes": []})
        return jsonify({"episodes": get_season_episodes(tmdb_id, season, _config.tmdb_api_key)})

    @app.route("/api/jobs/<int:job_id>/retry", methods=["GET", "POST"])
    def api_job_retry(job_id: int):
        """Resume a failed job from the furthest point that still has its files.

        GET reports what a retry would do without doing it — the difference
        between "moves the finished file" and "re-encodes for forty minutes" is
        worth knowing before pressing the button.
        """
        from adr import retry as _retry

        session = get_session()
        try:
            job = session.get(Job, job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404

            decision = _retry.plan(job, _config)
            if request.method == "GET":
                return jsonify(decision)

            if not decision["can_retry"]:
                return jsonify({"ok": False, "message": decision["reason"]}), 409

            if decision["resume"] == _retry.RESUME_TRANSFER:
                ok, message = _retry.retry_transfer(job, session, _config)
                return jsonify({"ok": ok, "message": message, "resume": "transfer"}), (
                    200 if ok else 409
                )

            if not _pipeline_manager:
                return jsonify({
                    "ok": False,
                    "message": "The encoder is not running, so nothing can be queued.",
                }), 503
            queued = _retry.requeue_encode(
                job, session, _config, _pipeline_manager.encode_queue,
            )
            return jsonify({
                "ok": bool(queued),
                "resume": "encode",
                "message": f"Re-queued {queued} file(s) for encoding.",
            })
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error("Retry failed for job %s: %s", job_id, exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            session.close()

    @app.route("/api/jobs/<int:job_id>/log")
    def api_job_log(job_id: int):
        """MakeMKV's and HandBrake's own output for this job.

        A failed rip otherwise shows one error string, with the tool's actual
        complaint sitting in journalctl behind two levels of shell.
        """
        session = get_session()
        try:
            if not session.get(Job, job_id):
                return jsonify({"error": "Job not found"}), 404
        finally:
            session.close()

        text = joblog.read(_config, job_id)
        return jsonify({
            "job_id": job_id,
            "log": text,
            "empty": not text.strip(),
        })

    @app.route("/api/jobs/<int:job_id>/files")
    def api_job_files(job_id: int):
        """List playable MP4 files for a completed job."""
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
                    files.append({
                        "name": f.name,
                        "size_mb": round(f.stat().st_size / BYTES_PER_MB, 1),
                    })
            return jsonify({"files": files, "title": job.display_title})
        finally:
            session.close()

    @app.route("/api/jobs/<int:job_id>/stream/<filename>")
    def api_stream_file(job_id: int, filename: str):
        """Stream an MP4 file for in-browser playback."""
        session = get_session()
        try:
            job = session.get(Job, job_id)
            if not job or not job.output_path:
                abort(404)
            out = Path(job.output_path)
            # Sanitise filename to prevent path traversal
            safe_name = Path(filename).name
            file_path = out / safe_name
            if not file_path.exists() or file_path.suffix.lower() != ".mp4":
                abort(404)
            # Ensure the resolved path is still inside the output dir
            if not file_path.resolve().is_relative_to(out.resolve()):
                abort(403)
            return send_file(file_path, mimetype="video/mp4", conditional=True)
        finally:
            session.close()

    @app.route("/api/tmdb/search")
    def api_tmdb_search():
        """Search TMDb for movies. Query params: q (search text), year (optional)."""
        query = request.args.get("q", "").strip()
        year = request.args.get("year", type=int)
        if not query:
            return jsonify({"error": "Missing query parameter 'q'"}), 400

        import requests as req
        api_key = _config.tmdb_api_key if _config else ""
        if not api_key:
            return jsonify({"error": "No TMDb API key configured"}), 400

        params = {
            "api_key": api_key,
            "query": query,
            "include_adult": "false",
            "language": "en-US",
        }
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
        """Manually re-match a job to a TMDb movie. Body: {"tmdb_id": 123}

        Works in any status — during ripping/encoding the metadata is
        updated immediately and the pipeline will pick up the new name
        when encoding finishes.  For already-completed jobs the output
        folder + files are renamed right away.
        """
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

            # Fetch movie details from TMDb
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

            # For finished jobs, rename output immediately.
            # For in-progress jobs, the pipeline renames when encoding finishes.
            if job.status in TERMINAL_STATUSES:
                from adr.pipeline import rename_job_output
                rename_job_output(job, session)
                logger.info(
                    "Re-matched job %s: '%s' -> '%s'",
                    job_id, old_title, job.display_title,
                )

                # Auto-flag for Plex move and trigger immediately for finished jobs
                if _config and _config.plex_path and _config.auto_move_to_plex:
                    job.move_to_plex = True
                    session.commit()
                    from adr.pipeline import move_to_plex
                    move_to_plex(job, session, _config)
            else:
                # For in-progress jobs, set the Plex flag (pipeline will move later)
                if _config and _config.plex_path and _config.auto_move_to_plex:
                    job.move_to_plex = True
                logger.info(
                    "Re-matched job %s: '%s' -> '%s' (metadata only, rename deferred)",
                    job_id, old_title, job.display_title,
                )

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
        """Overall system status."""
        if _pipeline_manager:
            return jsonify(_pipeline_manager.get_status())
        return jsonify({"drives": [], "encode_queue_size": 0, "encoder_workers": 0})

    @app.route("/api/jobs/<int:job_id>/toggle-plex", methods=["POST"])
    def api_toggle_plex(job_id: int):
        """Toggle the move_to_plex flag on a job."""
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
        """Manually move a completed job's output to the Plex library."""
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
            # Force the flag on so move_to_plex() proceeds
            job.move_to_plex = True
            session.commit()
            from adr.pipeline import move_to_plex
            ok = move_to_plex(job, session, _config)
            if ok:
                return jsonify({"ok": True, "plex_path": job.plex_path})
            else:
                return jsonify({"error": "Move failed — see log"}), 500
        except (OSError, SQLAlchemyError) as exc:
            session.rollback()
            logger.error("Failed to move job %s to Plex: %s", job_id, exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            session.close()

    @app.route("/api/system")
    def api_system():
        """System resource usage (CPU, RAM, disk, GPU if available)."""
        cpu_percent = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        # Disk usage for completed_path drive
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

        # Try to get GPU utilisation (NVIDIA via nvidia-smi)
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
            pass  # nvidia-smi not available — expected on non-NVIDIA systems
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
        """List available HandBrake presets (built-in + custom, cached 5 min)."""
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
        """Verify the configured HandBrake preset file and show its contents.

        Shares its implementation with the Doctor page's preset check — one
        answer to "is this preset usable", not two that can disagree.
        """
        from adr import diagnostics

        return jsonify(diagnostics.describe_preset(_config))

    @app.route("/api/settings", methods=["GET"])
    def api_get_settings():
        """Get current settings as JSON."""
        return jsonify(_config.as_dict())

    @app.route("/api/makemkv/refresh-key", methods=["POST"])
    def api_refresh_makemkv_key():
        """Fetch/refresh the MakeMKV registration key.

        Body (optional): {"key": "T-..."} to set an explicit key, otherwise the
        latest free beta key is fetched from the MakeMKV forum. The key is
        written to ~/.MakeMKV/settings.conf for the service user.
        """
        from adr import makemkv_key

        data = request.get_json(silent=True) or {}
        explicit = str(data.get("key", "")).strip() or None
        if explicit and not makemkv_key.is_valid_key(explicit):
            return jsonify({"ok": False, "error": "Key is malformed (expected T-...)"}), 400
        try:
            key = makemkv_key.ensure_key(explicit)
        except Exception as exc:  # noqa: BLE001 — surface any fetch/IO failure to the UI
            logger.warning("MakeMKV key refresh failed: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 500
        if not key:
            return jsonify({
                "ok": False,
                "error": "Could not obtain a key. Check internet access or paste one manually.",
            }), 502
        # Never echo the full secret back to the browser — just confirm + show a hint.
        return jsonify({"ok": True, "key_hint": key[:6] + "…" + key[-4:]})

    # ------------------------------------------------------------------ #
    # Storage / NAS
    #
    # This application cannot mount anything: it runs inside the container as
    # the unprivileged 'adr' user, while the mount belongs on the Proxmox host.
    # These endpoints are therefore strictly read-only — they report where
    # files are actually landing and generate the command to run on the host.
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Notifications and Plex
    # ------------------------------------------------------------------ #

    @app.route("/api/notify/test", methods=["POST"])
    def api_notify_test():
        """Send a test notification using the values on the settings form.

        Deliberately uses the posted values rather than the saved config: the
        point is to find out whether they work *before* saving them, which is
        the only moment testing is actually useful.
        """
        from adr import notify

        data = request.get_json() or {}
        ok, detail = notify.send(
            str(data.get("provider", "")),
            str(data.get("url", "")),
            "Automatic Disc Ripper",
            "Test notification — if you can read this, notifications work.",
            notify.EVENT_TEST,
            str(data.get("token", "")),
        )
        return jsonify({"ok": ok, "detail": detail}), (200 if ok else 400)

    @app.route("/api/plex/sections", methods=["POST"])
    def api_plex_sections():
        """List the Plex libraries, so the user picks one instead of guessing a key."""
        from adr import plex

        data = request.get_json() or {}
        sections, error = plex.list_sections(
            str(data.get("url", "")), str(data.get("token", "")),
        )
        if error:
            return jsonify({"ok": False, "error": error}), 400
        return jsonify({"ok": True, "sections": sections})

    @app.route("/api/plex/refresh", methods=["POST"])
    def api_plex_refresh():
        """Trigger a library scan now — also the 'does this work' test button."""
        from adr import plex

        data = request.get_json() or {}
        ok, detail = plex.refresh_section(
            str(data.get("url", "") or _config.plex_url),
            str(data.get("token", "") or _config.plex_token),
            str(data.get("section", "") or _config.plex_section),
        )
        return jsonify({"ok": ok, "detail": detail}), (200 if ok else 400)

    # ------------------------------------------------------------------ #
    # Doctor / updates
    # ------------------------------------------------------------------ #

    @app.route("/api/doctor")
    def api_doctor():
        """Everything the container can check about itself.

        The host-side checks (device cgroup, passthrough entries, boot
        ordering) need `pct` and are not reachable from in here; the page hands
        over the `adr-doctor` command for those instead of guessing.
        """
        from adr import diagnostics

        return jsonify(diagnostics.run_checks(_config))

    @app.route("/api/update/check")
    def api_update_check():
        """Compare the installed commit with the branch head on GitHub."""
        from adr import updater

        result = updater.check_for_update()
        supported, why = updater.updates_supported()
        result["can_apply"] = supported
        result["cannot_apply_reason"] = why
        return jsonify(result)

    @app.route("/api/update/start", methods=["POST"])
    def api_update_start():
        """Ask the root-side unit to apply the update.

        Deliberately takes no parameters. Repository and branch live in the
        systemd unit, so this endpoint cannot be talked into fetching code from
        somewhere else — it can only ask for the update the machine's owner
        already configured.
        """
        from adr import updater

        ok, message = updater.request_update()
        return jsonify({"ok": ok, "message": message}), (200 if ok else 409)

    @app.route("/api/update/status")
    def api_update_status():
        """How far the update has got, and what it has printed."""
        from adr import updater

        return jsonify(updater.update_status())

    @app.route("/api/drives/health")
    def api_drive_health():
        """Report whether the optical drives are actually usable in here.

        Distinguishes "the host has a drive this container cannot see" from
        "the node exists but the device cgroup denies it" — two problems that
        otherwise look identical from the dashboard.
        """
        from adr.disc import diagnose_passthrough

        return jsonify(diagnose_passthrough())

    @app.route("/api/drives/test", methods=["POST"])
    def api_drive_test():
        """Actively poke a drive and report every probe separately.

        The dashboard's passive view answers "is a disc loaded"; this answers
        "does this drive work", which is a different question and the one people
        actually have. ``deep`` additionally asks MakeMKV to open the disc —
        slow, and the only check that exercises the registration key.
        """
        from adr import drivetest
        from adr.disc import _sr_devices

        data = request.get_json(silent=True) or {}
        device = str(data.get("device", "")).strip()
        deep = bool(data.get("deep"))

        # Only devices this container can actually see. The endpoint takes a
        # path and opens it, so it must not accept an arbitrary one.
        if device not in _sr_devices():
            return jsonify({"error": f"Unknown optical device '{device}'."}), 400

        if not deep:
            return jsonify(drivetest.probe_drive(device, deep=False))

        # The deep probe allows five minutes, because that is what a Blu-ray
        # with many playlists needs. A phone browser gives up long before, and
        # then the page can only say "Load failed" — which reads as a broken
        # drive when the drive is fine and still working. Start it and let the
        # page ask how it is getting on.
        return jsonify(drivetest.start_probe(device, deep=True)), 202

    @app.route("/api/drives/test/status")
    def api_drive_test_status():
        """How the background probe of a drive is getting on."""
        from adr import drivetest

        device = (request.args.get("device") or "").strip()
        state = drivetest.probe_status(device)
        if state is None:
            return jsonify({"error": f"No probe has been run for '{device}'."}), 404
        return jsonify(state)

    @app.route("/api/drives/rescan", methods=["POST"])
    def api_drive_rescan():
        """Re-detect optical drives now, hot-adding anything new."""
        from adr import drivetest

        if _pipeline_manager:
            return jsonify(_pipeline_manager.rescan_drives())
        result = drivetest.rescan_drives()
        result["added"] = []
        result["known"] = result["devices"]
        return jsonify(result)

    @app.route("/api/storage")
    def api_storage():
        """Report the real state of the configured storage paths."""
        from adr import storage as _storage

        # With a Plex library configured and auto-move on, finished films go
        # straight there and never touch completed_path — so that is the path
        # whose free space, writability and mount state actually decide whether
        # a rip can succeed. Judging the wrong one gives a green page and a
        # failed job.
        to_plex = bool(_config.plex_path and _config.auto_move_to_plex)
        destination = _config.plex_path if to_plex else _config.completed_path

        staging = _storage.should_stage(destination, _config.stage_locally)
        paths = {
            "raw": _storage.describe_path(_config.raw_path),
            "completed": _storage.describe_path(_config.completed_path),
        }
        if staging:
            paths["staging"] = _storage.describe_path(_config.staging_path)
        if _config.plex_path:
            paths["plex"] = _storage.describe_path(_config.plex_path)

        completed = paths["plex"] if to_plex else paths["completed"]
        warnings = []

        # Keeping films on the container's own disk is a perfectly good setup,
        # so it is not a warning by itself — nagging about a valid choice trains
        # people to ignore the banner. It only becomes a problem once the user
        # has attached network storage (which is what require_completed_mount
        # records) and it is no longer there: then rips would quietly fill the
        # container disk instead of reaching the NAS.
        if _config.require_completed_mount and completed["exists"] and not completed["is_mount"]:
            warnings.append(
                f"{completed['path']} is not a mounted filesystem. The share "
                "is detached, so rips will refuse to start rather than fill "
                "the container disk. A bind-mount is captured when the "
                "container starts — if it was mounted afterwards, restart "
                "the container."
            )
        if completed["exists"] and not completed["writable"]:
            warnings.append(
                f"The service user (uid {_storage.SERVICE_UID}) cannot write to "
                f"{completed['path']} — every rip will fail at the final step."
            )
        if completed["free_gb"] is not None and completed["free_gb"] < 15:
            warnings.append(
                f"Only {completed['free_gb']} GB free — a dual-layer DVD needs "
                "about 8.5 GB of scratch plus the finished file."
            )

        return jsonify({
            "paths": paths,
            "warnings": warnings,
            "staging": staging,
            "service_uid": _storage.SERVICE_UID,
            "ctid": os.environ.get("ADR_CTID", "").strip() or None,
            "require_mount": _config.require_completed_mount,
            "destination": str(destination),
            "destination_is_plex": to_plex,
        })

    @app.route("/api/storage/probe", methods=["POST"])
    def api_storage_probe():
        """Check that a NAS is reachable before the user runs anything."""
        from adr import storage as _storage

        data = request.get_json() or {}
        result = _storage.probe_nas(
            str(data.get("kind", "")),
            str(data.get("host", "")),
        )
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.route("/api/storage/command", methods=["POST"])
    def api_storage_command():
        """Build the adr-setup-nas command for the user to run on the host.

        The password is never accepted or echoed here — it is filled in on the
        host, where it is actually needed.
        """
        from adr import storage as _storage

        data = request.get_json() or {}
        kind = str(data.get("kind", "")).lower()
        if kind not in _storage.NAS_PORTS:
            return jsonify({"error": "kind must be 'nfs' or 'smb'"}), 400
        host = str(data.get("host", "")).strip()
        share = str(data.get("share", "")).strip()
        if not host or not share:
            return jsonify({"error": "host and share are required"}), 400

        # The password is used to render the command and then dropped; it is
        # never written to the config, the database or the log.
        password = str(data.get("password", ""))
        command = _storage.build_setup_command(
            kind, host, share,
            ctid=str(data.get("ctid", "")).strip() or os.environ.get("ADR_CTID", "").strip() or None,
            username=str(data.get("username", "")).strip(),
            mountpoint=str(data.get("mountpoint", "")).strip(),
            password=password,
        )
        return jsonify({
            "command": command,
            "nas_url": _storage.build_nas_url(kind, host, share),
            "service_uid": _storage.SERVICE_UID,
            "contains_password": bool(password),
        })

    # Settings keys the UI is allowed to write; anything outside this set is
    # rejected, which stops unknown keys being injected into adr.yaml.
    #
    # It is NOT a privilege boundary. makemkv_path and handbrake_path are
    # deliberately writable because the Settings page exposes them, so anyone
    # who can reach this API can point them at another binary and have it
    # executed as the 'adr' user on the next job. The API is unauthenticated by
    # design, so the actual boundary is the network: keep the container on a
    # trusted LAN and never port-forward it (see README).
    _ALLOWED_SETTINGS_KEYS = frozenset({
        "makemkv_path", "handbrake_path", "raw_path", "completed_path",
        "min_title_length", "handbrake_preset", "handbrake_preset_file",
        "handbrake_extra_args", "max_encode_jobs", "transcode_enabled",
        "drives", "tmdb_api_key",
        "watch_path", "watch_output_path", "watch_interval", "web_host",
        "web_port", "log_level", "disabled_drives", "eject_after_rip",
        "no_eject_drives", "main_feature_only", "plex_path", "tv_path",
        "series_detection", "series_min_minutes", "series_max_minutes",
        "series_min_episodes", "skip_duplicates",
        "auto_move_to_plex", "drive_labels",
        "notify_enabled", "notify_provider", "notify_url", "notify_token",
        "notify_events",
        "plex_refresh_enabled", "plex_url", "plex_token", "plex_section",
        "require_completed_mount", "stage_locally", "staging_path",
        "audio_cd_enabled", "audio_cd_format", "audio_cd_mp3_bitrate",
        "music_path", "cdparanoia_path", "ffmpeg_path",
        "data_disc_enabled", "data_disc_path",
    })

    @app.route("/api/settings", methods=["POST"])
    def api_save_settings():
        """Update settings from JSON body."""
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        # Reject unknown keys outright — prevents config injection via the
        # unauthenticated API.
        unknown = set(data.keys()) - _ALLOWED_SETTINGS_KEYS
        if unknown:
            return jsonify({"error": f"Unknown setting(s): {', '.join(sorted(unknown))}"}), 400

        # Basic validation
        errors = []
        if "web_port" in data:
            try:
                p = int(data["web_port"])
                if not (1 <= p <= 65535):
                    errors.append("web_port must be 1\u201365535")
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
        if "log_level" in data and data["log_level"] not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            errors.append("Invalid log_level")
        if "audio_cd_format" in data and data["audio_cd_format"] not in ("flac", "mp3"):
            errors.append("audio_cd_format must be 'flac' or 'mp3'")
        if errors:
            return jsonify({"error": "; ".join(errors)}), 400

        _config.update(data)

        # Indicate whether a restart is needed for changes to take effect
        _RUNTIME_KEYS = {
            "disabled_drives", "eject_after_rip", "no_eject_drives",
            "tmdb_api_key", "log_level", "plex_path", "auto_move_to_plex",
            "drive_labels",
        }
        needs_restart = bool(set(data.keys()) - _RUNTIME_KEYS)
        return jsonify({"ok": True, "config": _config.as_dict(), "requires_restart": needs_restart})
