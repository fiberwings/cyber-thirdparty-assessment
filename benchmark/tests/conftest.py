import pytest

import bench.db as db_mod
from bench.config import settings


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point the results DB at a temp file and reset the engine singleton."""
    monkeypatch.setattr(settings, "BENCH_DB_PATH", str(tmp_path / "bench.sqlite"))
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_SessionLocal", None)
    yield
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_SessionLocal", None)
