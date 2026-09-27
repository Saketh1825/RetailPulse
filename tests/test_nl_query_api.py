"""Integration tests for POST /query against the real test database.

These prove the *whole* pipeline, not just the validator in isolation: LLM output (faked, since this
sandbox cannot reach a real LLM API -- see app/nl_sql/llm_client.py) -> validator -> read-only
executor -> audit log. A dedicated test at the bottom proves the database-level backstop directly,
with no app code involved at all, since that is the layer meant to hold even if every other layer
has a bug.
"""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.nl_sql.llm_client import FakeLLMClient
from app.routers.nl_query import get_sql_generator

pytestmark = pytest.mark.integration


@pytest.fixture
def app(loaded_db):
    """A fresh app per test: rate limiter / dependency overrides must not leak between tests."""
    from app.main import create_app
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def fake(sql=None, error=None):
    return lambda: FakeLLMClient(fixed_sql=sql, raise_error=error)


def last_log_row(loaded_db):
    with psycopg.connect(loaded_db["dsn"]) as conn:
        return conn.execute(
            "SELECT question, status, reject_reason, row_count FROM nl_query_log ORDER BY id DESC LIMIT 1"
        ).fetchone()


def test_valid_question_returns_rows_and_logs_ok(app, client, loaded_db):
    app.dependency_overrides[get_sql_generator] = fake("SELECT name, category FROM items ORDER BY name LIMIT 3")
    body = client.post("/query", json={"question": "list three items"}).json()
    assert body["columns"] == ["name", "category"]
    assert body["row_count"] == 3 and len(body["rows"]) == 3
    assert body["limit_applied"] == 3
    assert body["timings"]["total_ms"] >= 0

    q, status, reason, row_count = last_log_row(loaded_db)
    assert (q, status, reason, row_count) == ("list three items", "ok", None, 3)


def test_missing_limit_is_reported_in_notes(app, client, loaded_db):
    app.dependency_overrides[get_sql_generator] = fake("SELECT name FROM items")
    body = client.post("/query", json={"question": "all item names"}).json()
    assert any("LIMIT" in n for n in body["notes"])


@pytest.mark.parametrize("sql,expected_reason", [
    ("DROP TABLE items", "not_a_select"),
    ("DELETE FROM sales", "not_a_select"),
    ("SELECT * FROM pg_shadow", "non_whitelisted_table"),
    ("SELECT pg_sleep(5)", "dangerous_function"),
    ("SELECT 1; DROP TABLE items", "multiple_statements"),
])
def test_destructive_or_invalid_sql_is_rejected_and_logged(app, client, loaded_db, sql, expected_reason):
    app.dependency_overrides[get_sql_generator] = fake(sql)
    resp = client.post("/query", json={"question": "malicious"})
    assert resp.status_code == 422

    _, status, reason, row_count = last_log_row(loaded_db)
    assert status == "rejected" and reason == expected_reason and row_count is None


def test_llm_failure_returns_502_and_is_logged(app, client, loaded_db):
    app.dependency_overrides[get_sql_generator] = fake(error="the AI service could not be reached")
    resp = client.post("/query", json={"question": "anything"})
    assert resp.status_code == 502
    _, status, reason, _ = last_log_row(loaded_db)
    assert status == "llm_error" and "could not be reached" in reason


def test_llm_not_configured_returns_503_when_no_override(app, client, loaded_db, monkeypatch):
    # No dependency override: exercises the real get_sql_generator(), which must refuse cleanly
    # when LLM_API_KEY is unset, rather than trying (and failing) a real network call.
    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    resp = client.post("/query", json={"question": "anything"})
    assert resp.status_code == 503
    assert "not configured" in resp.json()["error"]["message"]
    get_settings.cache_clear()


def test_api_key_required_when_configured(loaded_db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("API_KEY", "s3cret")
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()
    app.dependency_overrides[get_sql_generator] = fake("SELECT 1 AS one LIMIT 1")
    with TestClient(app) as c:
        no_key = c.post("/query", json={"question": "question one"})
        assert no_key.status_code == 401

        wrong_key = c.post("/query", json={"question": "question one"}, headers={"X-API-Key": "nope"})
        assert wrong_key.status_code == 401

        right_key = c.post("/query", json={"question": "question one"}, headers={"X-API-Key": "s3cret"})
        assert right_key.status_code == 200
    monkeypatch.delenv("API_KEY", raising=False)
    get_settings.cache_clear()


def test_rate_limit_returns_429_after_the_configured_number_of_requests(loaded_db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("QUERY_RATE_LIMIT_PER_MINUTE", "2")
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()
    app.dependency_overrides[get_sql_generator] = fake("SELECT 1 AS one LIMIT 1")
    with TestClient(app) as c:
        assert c.post("/query", json={"question": "question one"}).status_code == 200
        assert c.post("/query", json={"question": "question two"}).status_code == 200
        third = c.post("/query", json={"question": "question three"})
        assert third.status_code == 429
        assert "retry after" in third.json()["error"]["message"]
    monkeypatch.delenv("QUERY_RATE_LIMIT_PER_MINUTE", raising=False)
    get_settings.cache_clear()


def test_question_too_long_is_rejected(app, client, loaded_db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("NL_QUESTION_MAX_CHARS", "20")
    get_settings.cache_clear()
    from app.main import create_app
    app2 = create_app()
    app2.dependency_overrides[get_sql_generator] = fake("SELECT 1 AS one LIMIT 1")
    with TestClient(app2) as c:
        resp = c.post("/query", json={"question": "x" * 21})
        assert resp.status_code == 422
    monkeypatch.delenv("NL_QUESTION_MAX_CHARS", raising=False)
    get_settings.cache_clear()


# --------------------------------------------------------------------------------------------
# Layer-3 proof: the database itself refuses writes and enforces the timeout, with NO app code
# involved. This is the backstop the validator (layer 2) is not required to be perfect for.
# --------------------------------------------------------------------------------------------

def test_readonly_role_refuses_writes_even_with_no_validator_involved(loaded_db):
    from tests.conftest import TEST_RO_URL
    with psycopg.connect(TEST_RO_URL, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM sales")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO items (name, category) VALUES ('x', 'y')")


def test_readonly_role_enforces_statement_timeout_even_with_no_validator_involved(loaded_db):
    from tests.conftest import TEST_RO_URL
    with psycopg.connect(TEST_RO_URL, autocommit=True) as conn:
        conn.execute("SET statement_timeout = '200ms'")
        with pytest.raises(psycopg.errors.QueryCanceled):
            conn.execute("SELECT pg_sleep(2)")
