"""Thin database layer over PostgreSQL (psycopg 3) and SQLite.

Production uses PostgreSQL. The offline test suite uses SQLite so it runs with
no server. SQL is written once with `?` placeholders; for PostgreSQL they are
rewritten to `%s`. Returned dates/timestamps are normalized to ISO strings and
booleans to bool so calling code behaves identically on both engines.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
import threading
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .config import ROOT

SCHEMA_PATH = ROOT / "db" / "schema.sql"
# Optional modules with their own tables; created by init_schema, untouched by drop_all.
EXTRA_SCHEMAS = [ROOT / "db" / "retail_staging.sql"]
_BOOL_COLUMNS = {"passed", "is_exception", "simulated", "is_product", "missing_customer", "missing_description",
                 "zero_price", "sub_penny", "possible_duplicate", "overlap_excluded", "overlap_conflict"}

VIEW_SQL = "SELECT * FROM reconciliation_results WHERE is_exception"


class Database:
    def __init__(self, url: str, autocommit: bool = False):
        self.url = url
        # One connection may be shared by Streamlit sessions; serialize access.
        self._lock = threading.RLock()
        if url.startswith(("postgresql://", "postgres://")):
            import psycopg  # imported lazily: not needed for offline tests

            self.dialect = "postgresql"
            self._conn = psycopg.connect(url, autocommit=autocommit)
        elif url.startswith("sqlite:///"):
            self.dialect = "sqlite"
            path = url[len("sqlite:///"):]
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False: Streamlit runs each rerun on a worker thread.
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.execute("PRAGMA foreign_keys = ON")
        else:
            raise ValueError(f"unsupported DATABASE_URL: {url}")

    # ------------------------------------------------------------------ core
    def _sql(self, sql: str) -> str:
        if self.dialect != "postgresql":
            return sql
        # psycopg uses %s placeholders, so literal % (e.g. LIKE 'x%') must be doubled.
        return sql.replace("%", "%%").replace("?", "%s")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._sql(sql), tuple(params))
            cur.close()

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        rows = [tuple(r) for r in rows]
        if not rows:
            return
        with self._lock:
            cur = self._conn.cursor()
            cur.executemany(self._sql(sql), rows)
            cur.close()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._sql(sql), tuple(params))
            cols = [d[0] for d in cur.description] if cur.description else []
            raw = cur.fetchall()
            cur.close()
        return [dict(zip(cols, (_norm(c, v) for c, v in zip(cols, r)))) for r in raw]

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        rows = self.query(sql, params)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator["Database"]:
        try:
            yield self
            self.commit()
        except Exception:
            self.rollback()
            raise

    # ------------------------------------------------------------------ DDL
    def init_schema(self) -> None:
        for path in [SCHEMA_PATH, *EXTRA_SCHEMAS]:
            for statement in _split_sql(path.read_text()):
                self.execute(statement)
        if self.dialect == "postgresql":
            self.execute(f"CREATE OR REPLACE VIEW reconciliation_exceptions AS {VIEW_SQL}")
        else:
            self.execute(f"CREATE VIEW IF NOT EXISTS reconciliation_exceptions AS {VIEW_SQL}")
        self.commit()

    def drop_all(self) -> None:
        tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA_PATH.read_text())
        self.execute("DROP VIEW IF EXISTS reconciliation_exceptions")
        for table in reversed(tables):
            suffix = " CASCADE" if self.dialect == "postgresql" else ""
            self.execute(f"DROP TABLE IF EXISTS {table}{suffix}")
        self.commit()

    # ------------------------------------------------------------------ upsert
    def upsert(self, table: str, row: dict[str, Any], conflict: Sequence[str]) -> None:
        self.upsert_many(table, [row], conflict)

    def upsert_many(self, table: str, rows: list[dict[str, Any]], conflict: Sequence[str]) -> None:
        """INSERT ... ON CONFLICT (...) DO UPDATE — idempotent on both engines."""
        if not rows:
            return
        cols = list(rows[0].keys())
        updates = [c for c in cols if c not in conflict]
        set_clause = ", ".join(f"{c} = excluded.{c}" for c in updates)
        action = f"DO UPDATE SET {set_clause}" if updates else "DO NOTHING"
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT ({', '.join(conflict)}) {action}"
        )
        self.executemany(sql, ([r[c] for c in cols] for r in rows))


def _norm(col: str, value: Any) -> Any:
    if isinstance(value, Decimal):
        # PostgreSQL returns NUMERIC for SUM(bigint); all money here is integer cents.
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:          # TIMESTAMP without time zone: keep as recorded
            return value.isoformat(sep=" ")
        return value.astimezone(dt.timezone.utc).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if col in _BOOL_COLUMNS and value is not None:
        return bool(value)
    if hasattr(value, "hex") and type(value).__name__ == "UUID":
        return str(value)
    return value


def _split_sql(ddl: str) -> list[str]:
    lines = [ln for ln in ddl.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def connect(url: str | None = None, autocommit: bool = False) -> Database:
    from .config import get_settings

    return Database(url or get_settings().database_url, autocommit=autocommit)


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
