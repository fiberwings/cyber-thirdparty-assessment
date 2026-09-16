from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


_engine: Engine = create_engine(
    f"sqlite:///{settings.db_path}",
    future=True,
    connect_args={"check_same_thread": False},
)


@event.listens_for(_engine, "connect")
def _enable_sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ANN001
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    # WAL allows concurrent readers, but writers still serialize. With
    # parallelized agent calls we may have several connections trying to commit
    # at once — wait briefly instead of immediately raising SQLITE_BUSY.
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_engine() -> Engine:
    return _engine


def get_session() -> Generator[Session, None, None]:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


_ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    # (table, column, type DDL — must be a constant default; SQLite forbids
    # expressions like CURRENT_TIMESTAMP in ALTER TABLE ADD COLUMN.)
    ("weakness", "source_document_id", "INTEGER REFERENCES document(id) ON DELETE CASCADE"),
    ("weakness", "unmatched", "BOOLEAN NOT NULL DEFAULT 1"),
    ("weakness", "kind_signal", "VARCHAR(40) NOT NULL DEFAULT ''"),
    ("weakness", "dedupe_key", "VARCHAR(120)"),
    ("weakness", "created_at", "DATETIME"),
    ("weakness", "origin", "VARCHAR(30) NOT NULL DEFAULT 'document'"),
    ("weakness", "evidence_refs", "JSON NOT NULL DEFAULT '[]'"),
    ("weakness", "origin_refs", "JSON NOT NULL DEFAULT '[]'"),
    ("document", "weakness_extracted_at", "DATETIME"),
    ("scenario", "origin_weakness_ids", "JSON NOT NULL DEFAULT '[]'"),
    ("assessment", "phase_state", "JSON NOT NULL DEFAULT '{}'"),
    ("assessment", "executive_summary", "JSON"),
    ("control_assessment", "unresolved_citations", "JSON NOT NULL DEFAULT '[]'"),
    # Accuracy program Phase 1 (R7/R8)
    ("assessment", "as_of_date", "VARCHAR(10)"),
    ("assessment", "standards_profile", "JSON NOT NULL DEFAULT '{}'"),
    ("control_assessment", "last_error", "TEXT"),
    ("control_assessment", "last_run_at", "DATETIME"),
    ("model_call", "cached", "BOOLEAN NOT NULL DEFAULT 0"),
    ("model_call", "cached_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("model_call", "reasoning_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("model_call", "cost_source", "VARCHAR(20) NOT NULL DEFAULT ''"),
    # Accuracy program Phase 3 (R3/R4): legacy rows are reported rows
    ("weakness", "status", "VARCHAR(20) NOT NULL DEFAULT 'confirmed'"),
    ("weakness", "review", "JSON"),
    # Accuracy program Phase 4 (thin R2)
    ("document", "attestation_profile", "JSON"),
    # Sequential workflow enforcement: durable per-document extraction state
    ("document", "weakness_task_id", "VARCHAR(36)"),
    ("document", "weakness_error", "TEXT"),
    # Attestation profile failure surfaced on the row (not a silent NULL profile)
    ("document", "attestation_profile_error", "TEXT"),
    # Liveness / streaming router: per-call telemetry and failure forensics
    ("model_call", "first_token_ms", "INTEGER"),
    ("model_call", "output_head", "TEXT"),
    ("model_call", "output_tail", "TEXT"),
    ("model_call", "finish_reason", "VARCHAR(30)"),
    ("model_call", "native_finish_reason", "VARCHAR(60)"),
    ("model_call", "provider", "VARCHAR(60)"),
    ("model_call", "generation_id", "VARCHAR(80)"),
    ("model_call", "attempts_json", "JSON"),
    ("task", "last_activity_at", "DATETIME"),
    # Structured task progress for the AI activity indicator
    ("task", "stats", "JSON NOT NULL DEFAULT '{}'"),
]

_POST_MIGRATION_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS ix_weakness_source_document_id ON weakness(source_document_id)",
    "CREATE INDEX IF NOT EXISTS ix_weakness_dedupe_key ON weakness(dedupe_key)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_weakness_dedupe ON weakness(assessment_id, dedupe_key)",
]


def _ensure_columns() -> None:
    """Idempotent additive migration for SQLite.

    The project does not use Alembic — schema additions ship as
    `ALTER TABLE … ADD COLUMN` statements run at startup. New databases get
    the columns via `Base.metadata.create_all`; existing dev databases get
    upgraded here. Only additive (column-add) changes belong in this list:
    any rename / type change still needs a manual migration.
    """
    with _engine.begin() as conn:
        for table, col, type_ddl in _ADDITIVE_COLUMNS:
            existing = {
                row[1]
                for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            }
            if col not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {type_ddl}")
        for stmt in _POST_MIGRATION_INDEXES:
            conn.exec_driver_sql(stmt)


def _backfill_phase_state() -> None:
    """Seed phase_state for legacy rows from current_phase.

    Existing assessments that predate the phase_state column have no per-phase
    timestamps. Use the assessment's current_phase to mark previous phases as
    completed (timestamped at updated_at) so the listing UI shows them as done
    instead of "all pending". Idempotent: only touches rows whose phase_state
    is empty.
    """
    # Ordering comes from app.workflow (the single source of truth); stale
    # stamps live in the same entries and are never backfilled.
    from app.workflow import LEGACY_PHASES_BY_CURRENT as PHASES_BY_CURRENT

    import json as _json
    with _engine.begin() as conn:
        rows = conn.exec_driver_sql(
            "SELECT id, current_phase, updated_at, phase_state FROM assessment"
        ).fetchall()
        for aid, current_phase, updated_at, phase_state_raw in rows:
            try:
                existing = _json.loads(phase_state_raw) if phase_state_raw else {}
            except (TypeError, ValueError):
                existing = {}
            if existing:
                continue
            done_phases = PHASES_BY_CURRENT.get(current_phase or "", [])
            if not done_phases:
                continue
            backfill = {
                phase: {
                    "started_at": updated_at,
                    "completed_at": updated_at,
                    "task_id": None,
                    "error": None,
                }
                for phase in done_phases
                if phase != "scoping"  # scoping is derived, not tracked here
            }
            if not backfill:
                continue
            conn.exec_driver_sql(
                "UPDATE assessment SET phase_state = ? WHERE id = ?",
                (_json.dumps(backfill), aid),
            )


def init_db() -> None:
    """Create all tables and the FTS5 virtual table for chunks."""
    from app import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(_engine)
    _ensure_columns()
    _backfill_phase_state()

    # FTS5 virtual table mirrors `chunk.text`. Kept in sync via triggers.
    with _engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
                text, content='chunk', content_rowid='id', tokenize='porter'
            );
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS chunk_ai AFTER INSERT ON chunk BEGIN
                INSERT INTO chunk_fts(rowid, text) VALUES (new.id, new.text);
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS chunk_ad AFTER DELETE ON chunk BEGIN
                INSERT INTO chunk_fts(chunk_fts, rowid, text) VALUES ('delete', old.id, old.text);
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS chunk_au AFTER UPDATE ON chunk BEGIN
                INSERT INTO chunk_fts(chunk_fts, rowid, text) VALUES ('delete', old.id, old.text);
                INSERT INTO chunk_fts(rowid, text) VALUES (new.id, new.text);
            END;
            """
        )
