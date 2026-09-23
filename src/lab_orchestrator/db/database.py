"""Connection/engine handling for the SQLite DB.

Sync SQLAlchemy engine + the stdlib sqlite3 driver — not an async driver
like aiosqlite. Deliberate: architecture doc §9 already frames this as
"modest host resources, tiny concurrency, explicitly a dev/eval system,"
and the max-3-global quota trigger and per-user partial index
(db/models.py) lean on SQLite's ordinary single-writer locking, which is
simplest to reason about with synchronous connections. When M5 calls
into this from async request handlers, it wraps the call in a
thread-pool executor (`anyio.to_thread.run_sync` /
`starlette.concurrency.run_in_threadpool`) rather than switching drivers.

FK enforcement and WAL mode are both off by default in SQLite and must
be turned on per-connection — done here via a `connect` event listener,
not left to whoever happens to open a connection to remember.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine, create_engine

from lab_orchestrator.core.config import get_settings


def create_db_engine(db_path: Path | str) -> Engine:
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.close()

    return engine


@lru_cache
def get_engine() -> Engine:
    return create_db_engine(get_settings().db_path)
