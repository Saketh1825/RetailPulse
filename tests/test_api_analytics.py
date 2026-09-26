"""API tests for the analytics endpoints, against the real test database."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client(loaded_db):
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c


def sql(loaded_db, query, params=None):
    with psycopg.connect(loaded_db["dsn"]) as conn:
        return conn.execute(query, params).fetchall()


def test_health_reports_both_database_roles(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["database"] == "ok" and body["readonly_database"] == "ok"
    assert body["llm_configured"] is False and "key" not in str(body).lower().replace("llm_configured", "")


def test_summary_matches_database(client, loaded_db):
    body = client.get("/analytics/summary").json()
    (rows, rev), = sql(loaded_db, "SELECT count(*), sum(revenue) FROM sales")
    assert body["sales_rows"] == rows == loaded_db["report"].rows_loaded
    assert body["total_revenue"] == pytest.approx(float(rev), abs=0.01) and body["items"] == 24


def test_top_items_sorted_ranked_and_correct(client, loaded_db):
    body = client.get("/analytics/top-items", params={"limit": 5}).json()
    assert [r["rank"] for r in body] == [1, 2, 3, 4, 5]
    revs = [r["revenue"] for r in body]
    assert revs == sorted(revs, reverse=True)
    expected = sql(loaded_db, "SELECT i.name FROM sales s JOIN items i ON i.id=s.item_id GROUP BY i.name "
                              "ORDER BY sum(s.revenue) DESC, i.name LIMIT 5")
    assert [r["name"] for r in body] == [e[0] for e in expected]


def test_top_items_by_units_and_date_window(client):
    a = client.get("/analytics/top-items", params={"metric": "units", "limit": 3}).json()
    assert [r["units_sold"] for r in a] == sorted([r["units_sold"] for r in a], reverse=True)
    b = client.get("/analytics/top-items", params={"limit": 100, "start_date": "2023-06-01", "end_date": "2023-06-30"}).json()
    full = client.get("/analytics/top-items", params={"limit": 100}).json()
    assert sum(r["revenue"] for r in b) < sum(r["revenue"] for r in full)


def test_revenue_by_category_shares_sum_to_100(client):
    body = client.get("/analytics/revenue-by-category").json()
    assert len(body) == 6
    assert sum(r["revenue_share_pct"] for r in body) == pytest.approx(100, abs=0.1)
    assert body[0]["revenue"] >= body[-1]["revenue"]


def test_monthly_revenue_growth_is_computed_by_sql_window(client):
    body = client.get("/analytics/monthly-revenue").json()
    assert len(body) == 24 and body[0]["mom_growth_pct"] is None
    prev, cur = body[3], body[4]
    assert cur["mom_growth_pct"] == pytest.approx(100 * (cur["revenue"] - prev["revenue"]) / prev["revenue"], abs=0.01)
    dairy = client.get("/analytics/monthly-revenue", params={"category": "Dairy"}).json()
    assert dairy[0]["revenue"] < body[0]["revenue"]


def test_data_quality_endpoint_exposes_the_etl_audit(client, loaded_db):
    body = client.get("/data-quality/latest").json()
    assert body["status"] == "success" and body["rows_read"] == loaded_db["report"].rows_read
    assert body["rejection_summary"]["duplicate_row"] == loaded_db["manifest"]["injected_rejections"]["duplicate_row"]
    assert 3.5 < body["rejection_rate_pct"] < 4.5


@pytest.mark.parametrize("path, params", [
    ("/analytics/top-items", {"limit": 0}), ("/analytics/top-items", {"limit": 1000}),
    ("/analytics/top-items", {"metric": "profit"}), ("/analytics/top-items", {"start_date": "not-a-date"}),
    ("/analytics/top-items", {"start_date": "2024-02-01", "end_date": "2024-01-01"}),
])
def test_invalid_parameters_get_a_clean_422_envelope(client, path, params):
    r = client.get(path, params=params)
    assert r.status_code == 422 and r.json()["error"]["code"] in {"invalid_request", "http_error"}


def test_sql_injection_attempt_in_parameters_is_harmless(client, loaded_db):
    r = client.get("/analytics/monthly-revenue", params={"category": "Dairy'; DROP TABLE sales; --"})
    assert r.status_code == 200 and r.json() == []                       # just "no such category"
    assert sql(loaded_db, "SELECT count(*) FROM sales")[0][0] == loaded_db["report"].rows_loaded


def test_unknown_route_uses_error_envelope(client):
    r = client.get("/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
