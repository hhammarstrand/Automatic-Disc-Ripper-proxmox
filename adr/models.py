"""SQLAlchemy database models for Automatic Disc Ripper.

Tracks ripping/encoding jobs and individual tracks (titles) per disc.
"""

import enum

from sqlalchemy import Boolean, Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

from adr.config import DATABASE_PATH
from adr.utils import utcnow

Base = declarative_base()


class JobStatus(enum.Enum):
    """Lifecycle states for a ripping/encoding job."""
    PENDING = "pending"
    IDENTIFYING = "identifying"
    RIPPING = "ripping"
    RIPPED = "ripped"
    ENCODING = "encoding"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"


class TrackStatus(enum.Enum):
    """Lifecycle states for an individual track."""
    PENDING = "pending"
    ENCODING = "encoding"
    DONE = "done"
    ERROR = "error"


# ------------------------------------------------------------------ #
# Convenience status sets (avoid duplicating lists across modules)
# ------------------------------------------------------------------ #

#: Statuses where the job is still actively being processed.
ACTIVE_STATUSES = frozenset({
    JobStatus.PENDING,
    JobStatus.IDENTIFYING,
    JobStatus.RIPPING,
    JobStatus.RIPPED,
    JobStatus.ENCODING,
})

#: Statuses for the rip phase (disc is still in the drive).
RIP_PHASE_STATUSES = frozenset({
    JobStatus.PENDING,
    JobStatus.IDENTIFYING,
    JobStatus.RIPPING,
})

#: Statuses for the encode phase.
ENCODE_PHASE_STATUSES = frozenset({
    JobStatus.RIPPED,
    JobStatus.ENCODING,
})

#: Terminal statuses — the job is finished (successfully or not).
TERMINAL_STATUSES = frozenset({
    JobStatus.DONE,
    JobStatus.ERROR,
    JobStatus.CANCELLED,
})


class Job(Base):
    """Represents a single disc rip+encode pipeline run."""
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    disc_label = Column(String(255), nullable=True)
    title = Column(String(255), nullable=True)
    year = Column(Integer, nullable=True)
    tmdb_id = Column(Integer, nullable=True)
    poster_url = Column(String(512), nullable=True)
    drive = Column(String(10), nullable=False)
    status = Column(Enum(JobStatus), nullable=False, default=JobStatus.PENDING)
    progress_rip = Column(Float, default=0.0)      # 0.0 – 1.0
    progress_encode = Column(Float, default=0.0)    # 0.0 – 1.0
    progress_info = Column(Text, nullable=True)      # JSON: rich progress detail
    output_path = Column(String(1024), nullable=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, default=utcnow)
    rip_completed_at = Column(DateTime, nullable=True)
    encode_started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    avg_fps = Column(Float, nullable=True)
    move_to_plex = Column(Boolean, nullable=True, default=None)
    plex_path = Column(String(1024), nullable=True)
    # The id of an earlier completed job for the same disc label, when one
    # exists. Annotation only — a disc label is not a unique identifier, so
    # this never blocks a rip.
    duplicate_of = Column(Integer, nullable=True)

    # Television. A disc is a film unless told otherwise: that is the common
    # case, and guessing wrong towards "series" would rename a film into a
    # season folder, which is far more annoying to undo than the reverse.
    content_type = Column(String(16), nullable=False, default="movie")  # movie | series
    series_season = Column(Integer, nullable=True)
    series_first_episode = Column(Integer, nullable=True)

    tracks = relationship("Track", back_populates="job", cascade="all, delete-orphan")

    # -------------------------------------------------------------- #
    # Convenience helpers
    # -------------------------------------------------------------- #

    @property
    def display_title(self) -> str:
        """Human-readable title like 'Movie Title (2023)' or disc label."""
        if self.title and self.year:
            return f"{self.title} ({self.year})"
        return self.title or self.disc_label or f"Job #{self.id}"

    @property
    def progress(self) -> float:
        """Overall progress across rip + encode (0.0 – 1.0)."""
        if self.status == JobStatus.DONE:
            return 1.0
        # Weight: rip=40%, encode=60%
        return (self.progress_rip or 0.0) * 0.4 + (self.progress_encode or 0.0) * 0.6

    @property
    def phase_progress(self) -> float:
        """Progress for the *current* phase (0.0 – 1.0).

        During ripping returns rip progress; during encoding returns
        encode progress.  This is what the UI should show so the bar
        goes 0–100% for each phase instead of a confusing weighted mix.
        """
        if self.status in (JobStatus.RIPPING, JobStatus.IDENTIFYING):
            return self.progress_rip or 0.0
        if self.status in (JobStatus.ENCODING, JobStatus.RIPPED):
            return self.progress_encode or 0.0
        if self.status == JobStatus.DONE:
            return 1.0
        return 0.0

    @property
    def rip_duration(self) -> int | None:
        """Rip duration in seconds, or None if not available."""
        if self.started_at and self.rip_completed_at:
            return int((self.rip_completed_at - self.started_at).total_seconds())
        return None

    @property
    def encode_duration(self) -> int | None:
        """Encode duration in seconds, or None if not available."""
        if self.encode_started_at and self.completed_at:
            return int((self.completed_at - self.encode_started_at).total_seconds())
        return None

    @property
    def total_duration(self) -> int | None:
        """Total pipeline duration in seconds, or None if not finished."""
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds())
        return None

    def to_dict(self) -> dict:
        """Serialise to JSON-friendly dict for the API."""
        import json as _json
        try:
            pi = _json.loads(self.progress_info) if self.progress_info else None
        except (ValueError, TypeError):
            pi = None
        return {
            "id": self.id,
            "disc_label": self.disc_label,
            "title": self.title,
            "year": self.year,
            "tmdb_id": self.tmdb_id,
            "poster_url": self.poster_url,
            "drive": self.drive,
            "status": self.status.value if self.status else "unknown",
            "progress_rip": round(self.progress_rip or 0.0, 4),
            "progress_encode": round(self.progress_encode or 0.0, 4),
            "progress": round(self.progress or 0.0, 4),
            "phase_progress": round(self.phase_progress or 0.0, 4),
            "progress_info": pi,
            "display_title": self.display_title,
            "output_path": self.output_path,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "rip_completed_at": self.rip_completed_at.isoformat() if self.rip_completed_at else None,
            "encode_started_at": self.encode_started_at.isoformat() if self.encode_started_at else None,
            "avg_fps": round(self.avg_fps, 1) if self.avg_fps else None,
            "rip_duration": self.rip_duration,
            "encode_duration": self.encode_duration,
            "total_duration": self.total_duration,
            "move_to_plex": self.move_to_plex,
            "plex_path": self.plex_path,
            "tracks": [t.to_dict() for t in self.tracks],
        }

    def __repr__(self) -> str:
        st = self.status.value if self.status else 'unknown'
        return f"<Job {self.id} {self.display_title} [{st}]>"


class Track(Base):
    """Single title/track extracted from a disc."""
    __tablename__ = "tracks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    track_number = Column(Integer, nullable=False)
    filename = Column(String(512), nullable=True)
    size_mb = Column(Float, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    status = Column(Enum(TrackStatus), nullable=False, default=TrackStatus.PENDING)
    output_path = Column(String(1024), nullable=True)
    # Why this track failed. The job used to carry only "one or more tracks
    # failed to encode", which names the symptom and nothing else — on a disc
    # with several titles it does not even say which one.
    error_message = Column(Text, nullable=True)
    # Which episode this track holds, for a series job. Stored rather than
    # recomputed so the mapping survives a restart mid-encode.
    episode_number = Column(Integer, nullable=True)

    job = relationship("Job", back_populates="tracks")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "track_number": self.track_number,
            "filename": self.filename,
            "size_mb": round(self.size_mb, 1) if self.size_mb else None,
            "duration_seconds": self.duration_seconds,
            "status": self.status.value if self.status else "unknown",
            "output_path": self.output_path,
            "episode_number": self.episode_number,
        }

    def __repr__(self) -> str:
        return f"<Track {self.track_number} job={self.job_id} [{self.status.value}]>"


# ------------------------------------------------------------------ #
# Database initialisation helpers
# ------------------------------------------------------------------ #

_engine = None
_SessionFactory = None


def get_engine():
    global _engine
    if _engine is None:
        db_url = f"sqlite:///{DATABASE_PATH}"
        _engine = create_engine(
            db_url, echo=False, future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        # WAL mode + busy_timeout allow concurrent reads while writing —
        # prevents "database is locked" when encoder workers + Flask read
        # simultaneously.
        from sqlalchemy import event

        @event.listens_for(_engine, "connect")
        def _set_sqlite_wal(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return _engine


def _migrate_db(engine) -> None:
    """Apply lightweight schema migrations for columns added after initial release."""
    import logging
    _log = logging.getLogger(__name__)
    try:
        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            cols = {row[1] for row in cur.execute("PRAGMA table_info(jobs)").fetchall()}
            _new_cols = [
                ("progress_info", "TEXT"),
                ("rip_completed_at", "DATETIME"),
                ("encode_started_at", "DATETIME"),
                ("avg_fps", "FLOAT"),
                ("move_to_plex", "BOOLEAN"),
                ("plex_path", "VARCHAR(1024)"),
                ("duplicate_of", "INTEGER"),
                ("content_type", "VARCHAR(16) DEFAULT 'movie'"),
                ("series_season", "INTEGER"),
                ("series_first_episode", "INTEGER"),
            ]
            for col_name, col_type in _new_cols:
                if col_name not in cols:
                    cur.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}")
                    _log.info("Migration: added '%s' column to jobs table", col_name)

            track_cols = {row[1] for row in cur.execute("PRAGMA table_info(tracks)").fetchall()}
            for col_name, col_type in [("episode_number", "INTEGER"),
                                       ("error_message", "TEXT")]:
                if col_name not in track_cols:
                    cur.execute(f"ALTER TABLE tracks ADD COLUMN {col_name} {col_type}")
                    _log.info("Migration: added '%s' column to tracks table", col_name)
            raw.commit()
        finally:
            raw.close()
    except Exception as exc:
        _log.error("Migration failed: %s — tables may be incomplete", exc)


def _check_db_integrity(engine) -> bool:
    """Run PRAGMA integrity_check on the database. Returns True if OK."""
    import logging
    _log = logging.getLogger(__name__)
    try:
        raw = engine.raw_connection()
        try:
            result = raw.cursor().execute("PRAGMA integrity_check").fetchone()
            if result and result[0] == "ok":
                return True
            _log.error("Database integrity check FAILED: %s", result)
            return False
        finally:
            raw.close()
    except Exception as exc:
        _log.error("Database integrity check raised exception: %s", exc)
        return False


def init_db() -> None:
    """Create all tables if they don't exist, then apply migrations.

    If the database file is corrupt, it is backed up and recreated
    automatically so the application can continue running.
    """
    import logging
    import os
    import shutil
    _log = logging.getLogger(__name__)

    engine = get_engine()

    # Check integrity of existing database
    if os.path.exists(DATABASE_PATH) and os.path.getsize(DATABASE_PATH) > 0:  # noqa: SIM102
        if not _check_db_integrity(engine):
            _log.warning("Database is corrupt — backing up and recreating")
            # Dispose engine connections so the file can be moved
            engine.dispose()
            backup = str(DATABASE_PATH) + ".corrupt"
            try:
                shutil.move(str(DATABASE_PATH), backup)
                _log.info("Corrupt database backed up to %s", backup)
            except OSError as exc:
                _log.warning("Could not move corrupt DB: %s — deleting instead", exc)
                os.remove(str(DATABASE_PATH))
            # Remove WAL/journal files too
            for suffix in ("-wal", "-shm", "-journal"):
                p = str(DATABASE_PATH) + suffix
                if os.path.exists(p):
                    os.remove(p)
            # Reset engine so it reconnects to the new (empty) file
            global _engine, _SessionFactory
            _engine = None
            _SessionFactory = None
            engine = get_engine()

    Base.metadata.create_all(engine)
    _migrate_db(engine)


def get_session() -> Session:
    """Return a new SQLAlchemy session."""
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine())
    return _SessionFactory()
