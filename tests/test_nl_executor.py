"""Integration tests for app/nl_sql/executor.py against the real test database, using the actual
SELECT-only role (not a mock) so the permission-denied path is exercised for real, not simulated."""
from __future__ import annotations

import psycopg
import pytest

from app.nl_sql.executor import ExecutionError, execute_readonly

pytestmark = pytest.mark.integration


@pytest.fixture
def ro_conn(loaded_db):
    from tests.conftest import TEST_RO_URL
    with psycopg.connect(TEST_RO_URL, row_factory=psycopg.rows.dict_row) as conn:
        yield conn


def test_execute_returns_columns_and_rows(ro_conn):
    result = execute_readonly(ro_conn, "SELECT name, category FROM items ORDER BY name LIMIT 4", limit_applied=3)
    assert result.columns == ["name", "category"]
    assert result.row_count == 3            # capped to limit_applied, not the raw LIMIT 4
    assert result.truncated is True          # 4 rows came back but only 3 were requested
    assert result.elapsed_ms >= 0


def test_execute_reports_not_truncated_when_fewer_rows_than_cap(ro_conn):
    result = execute_readonly(ro_conn, "SELECT 1 AS one LIMIT 6", limit_applied=5)
    assert result.row_count == 1
    assert result.truncated is False


def test_execute_raises_on_permission_denied_even_if_validator_is_bypassed(ro_conn):
    # This directly proves the layer-3 backstop from inside the executor itself: even code that
    # forgot to call the validator cannot turn into a write, because the connection's role can't.
    with pytest.raises(ExecutionError) as exc_info:
        execute_readonly(ro_conn, "DELETE FROM sales", limit_applied=1)
    assert exc_info.value.code == "permission_denied"


def test_execute_raises_query_timeout(ro_conn):
    ro_conn.execute("SET statement_timeout = '150ms'")
    ro_conn.commit()
    with pytest.raises(ExecutionError) as exc_info:
        execute_readonly(ro_conn, "SELECT pg_sleep(2)", limit_applied=1)
    assert exc_info.value.code == "query_timeout"


def test_connection_is_usable_again_after_an_error(ro_conn):
    with pytest.raises(ExecutionError):
        execute_readonly(ro_conn, "DELETE FROM sales", limit_applied=1)
    # the executor must roll back after a failure so the pooled connection can be reused
    result = execute_readonly(ro_conn, "SELECT 1 AS one LIMIT 1", limit_applied=1)
    assert result.row_count == 1
