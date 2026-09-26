"""Create the schema and the read-only role.  Usage:  python -m scripts.init_db

Uses ADMIN_DATABASE_URL if set, otherwise DATABASE_URL (the database owner). The read-only role's
name and password are taken from READONLY_DATABASE_URL, so there is a single source of truth and
no password is ever written into a .sql file.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from app.config import PROJECT_ROOT, get_settings

log = logging.getLogger("init_db")


def init_db(admin_url: str | None = None) -> None:
    settings = get_settings()
    admin_url = admin_url or os.environ.get("ADMIN_DATABASE_URL") or settings.database_url
    ro = conninfo_to_dict(settings.readonly_database_url)
    role, password = ro.get("user"), ro.get("password")
    if not role or not password:
        sys.exit("READONLY_DATABASE_URL must include a user and password")

    with psycopg.connect(admin_url, autocommit=True) as conn:
        dbname = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute((PROJECT_ROOT / "db" / "schema.sql").read_text())
        log.info("schema applied to database %s", dbname)

        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if exists:   # PG16: a non-superuser owner may not (re)state SUPERUSER-family attributes
            stmt = "ALTER ROLE {role} WITH PASSWORD {pw} CONNECTION LIMIT 10"
        else:
            stmt = ("CREATE ROLE {role} WITH LOGIN PASSWORD {pw} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 10")
        conn.execute(sql.SQL(stmt).format(role=sql.Identifier(role), pw=sql.Literal(password)))
        grants = Path(PROJECT_ROOT / "db" / "readonly_grants.sql").read_text()
        conn.execute(sql.SQL(grants).format(role=sql.Identifier(role), dbname=sql.Identifier(dbname)))
        log.info("read-only role %r configured (SELECT on items, sales, forecasts only)", role)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    init_db()
