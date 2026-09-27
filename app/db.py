"""Database connection pools.

Two pools, two roles, on purpose:
  * rw_pool -- the normal application role (ETL owner). Used by analytics/forecast/audit code.
  * ro_pool -- the SELECT-only role. Used ONLY to execute LLM-generated SQL (app/nl_sql/executor.py).
Keeping them as separate objects makes it structurally hard to run generated SQL on the wrong connection.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

from fastapi import Request
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("app.db")


def make_pool(dsn: str, *, name: str, max_size: int = 5, options: str = "") -> ConnectionPool:
    pool = ConnectionPool(
        dsn, name=name, min_size=1, max_size=max_size, open=False, timeout=5,
        kwargs={"row_factory": dict_row, "options": options, "application_name": f"retailpulse-{name}"},
    )
    pool.open(wait=False)          # never block/crash startup if the DB is briefly unavailable
    return pool


def get_conn(request: Request) -> Iterator[Connection]:
    """FastAPI dependency: a pooled READ/WRITE connection (commits on success, rolls back on error)."""
    with request.app.state.rw_pool.connection() as conn:
        yield conn


def get_ro_conn(request: Request) -> Iterator[Connection]:
    """FastAPI dependency: a pooled connection using the SELECT-only PostgreSQL role. Used ONLY by
    app/nl_sql/executor.py to run LLM-generated SQL. Never use this pool for anything the app itself
    needs to write -- that is the whole point of it being a separate, structurally distinct pool."""
    with request.app.state.ro_pool.connection() as conn:
        yield conn
