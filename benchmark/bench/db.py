"""Engine/session factory for the benchmark results DB."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine(db_path: str | None = None):
    global _engine, _SessionLocal
    if _engine is None:
        path = Path(db_path or settings.BENCH_DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(_engine)
        _migrate(_engine)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_session() -> Session:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def _migrate(engine) -> None:
    """Additive-only migration: add columns that exist on the models but not
    in an older results DB (create_all never alters existing tables)."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                coltype = col.type.compile(engine.dialect)
                conn.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}')
                )
