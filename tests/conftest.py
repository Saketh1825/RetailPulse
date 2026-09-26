"""Shared test fixtures.

SAFETY: tests run against a dedicated database whose name must end in `_test`. The suite refuses to run
against anything else, so it can never wipe the demo database. PostgreSQL-dependent tests are marked
`integration` and are skipped automatically (not failed) when no server is reachable.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://retailpulse:retailpulse@localhost:5432/retailpulse_test")
TEST_RO_URL = os.environ.get(
    "TEST_READONLY_DATABASE_URL",
    "postgresql://retailpulse_readonly:readonly_dev_password@localhost:5432/retailpulse_test",
)
assert psycopg.conninfo.conninfo_to_dict(TEST_DB_URL)["dbname"].endswith("_test"), \
    "refusing to run tests against a database whose name does not end in _test"

# Must happen BEFORE the app's settings are first read.
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["READONLY_DATABASE_URL"] = TEST_RO_URL
os.environ["LLM_API_KEY"] = ""
os.environ["API_KEY"] = ""


def _reachable(url: str) -> bool:
    try:
        psycopg.connect(url, connect_timeout=3).close()
        return True
    except psycopg.Error:
        return False


@pytest.fixture(scope="session")
def db_url() -> str:
    if not _reachable(TEST_DB_URL):
        pytest.skip("PostgreSQL test database not reachable")
    from scripts.init_db import init_db

    with psycopg.connect(TEST_DB_URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS nl_query_log, etl_runs, forecast_models, forecasts, sales, items CASCADE")
    init_db(TEST_DB_URL)
    return TEST_DB_URL


@pytest.fixture(scope="session")
def dataset(tmp_path_factory):
    """Two years of synthetic data with injected dirt + its ground-truth manifest."""
    from scripts.generate_sample_data import generate_clean, inject_dirt

    clean = generate_clean("2022-01-01", "2023-12-31", seed=42)
    dirty, manifest = inject_dirt(clean, seed=42)
    path: Path = tmp_path_factory.mktemp("data") / "sales_raw.csv"
    dirty.to_csv(path, index=False)
    return {"csv": path, "manifest": manifest, "clean": clean}


@pytest.fixture(scope="session")
def loaded_db(db_url, dataset, tmp_path_factory):
    """The test database after one ETL run of the dirty dataset."""
    from etl.pipeline import run_etl

    report = run_etl(dataset["csv"], dsn=db_url, rejected_dir=tmp_path_factory.mktemp("rejected"))
    return {"dsn": db_url, "report": report, **dataset}
