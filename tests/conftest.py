from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from finrecon import pipeline  # noqa: E402
from finrecon.config import get_settings  # noqa: E402
from finrecon.db import Database  # noqa: E402
from finrecon.ingestion.runner import run_id_for  # noqa: E402


def no_sleep(_seconds: float) -> None:
    pass


# Set TEST_DATABASE_URL=postgresql://user:pw@host:5432/postgres to run the suite on
# PostgreSQL (CI does). Each fixture then gets its own freshly created database.
PG_ADMIN_URL = os.environ.get("TEST_DATABASE_URL")


def _database_url(tmp_path: Path) -> str:
    if not PG_ADMIN_URL:
        return f"sqlite:///{tmp_path / 'test.db'}"
    import psycopg

    name = "finrecon_test_" + uuid.uuid4().hex[:10]
    with psycopg.connect(PG_ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    return PG_ADMIN_URL.rsplit("/", 1)[0] + "/" + name


def _fresh(tmp_path: Path):
    settings = get_settings(raw_dir=tmp_path / "raw", database_url=_database_url(tmp_path), mode="offline")
    db = Database(settings.database_url)
    pipeline.demo(settings, db, sleep=no_sleep)
    return db, settings, run_id_for(settings.recon_as_of, 2)


@pytest.fixture(scope="session")
def demo(tmp_path_factory):
    """Read-only shared demo database. Tests that mutate data use `fresh_demo`."""
    db, settings, run_id = _fresh(tmp_path_factory.mktemp("demo"))
    yield db, settings, run_id
    db.close()


@pytest.fixture
def fresh_demo(tmp_path):
    db, settings, run_id = _fresh(tmp_path)
    yield db, settings, run_id
    db.close()


def status_of(db: Database, run_id: str, record_type: str, scenario_prefix: str) -> list[str]:
    rows = db.query("SELECT status FROM reconciliation_results WHERE run_id = ? AND record_type = ? "
                    "AND scenario_id LIKE ? ORDER BY scenario_id", (run_id, record_type, scenario_prefix + "%"))
    return [r["status"] for r in rows]
