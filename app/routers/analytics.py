"""SQL-backed analytics endpoints.

All SQL is parameterised (psycopg sends values separately from the statement), and the only thing ever
interpolated into a statement is a fixed constant chosen from a dict -- never user input.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import Connection

from app.db import get_conn
from app.models import CategoryRevenue, DataQualityRun, ItemOut, MonthlyRevenue, Summary, TopItem

router = APIRouter(tags=["analytics"])
Conn = Annotated[Connection, Depends(get_conn)]

_ORDER = {"revenue": "SUM(s.revenue)", "units": "SUM(s.units_sold)"}     # whitelist -> safe to interpolate


def _date_filter(start: date | None, end: date | None) -> tuple[str, dict]:
    if start and end and start > end:
        raise HTTPException(422, "start_date must be on or before end_date")
    clauses, params = [], {}
    if start:
        clauses.append("s.sale_date >= %(start)s"); params["start"] = start
    if end:
        clauses.append("s.sale_date <= %(end)s"); params["end"] = end
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


@router.get("/analytics/summary", response_model=Summary, summary="Headline numbers for the loaded data")
def summary(conn: Conn):
    return conn.execute(
        "SELECT (SELECT count(*) FROM items)::int AS items, count(*)::int AS sales_rows, "
        "min(sale_date) AS first_date, max(sale_date) AS last_date, "
        "COALESCE(sum(units_sold), 0)::bigint AS total_units, COALESCE(sum(revenue), 0) AS total_revenue "
        "FROM sales").fetchone()


@router.get("/analytics/top-items", response_model=list[TopItem], summary="Top-selling items")
def top_items(
    conn: Conn,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    metric: Annotated[Literal["revenue", "units"], Query(description="Rank by revenue or by units sold")] = "revenue",
    start_date: date | None = None,
    end_date: date | None = None,
):
    where, params = _date_filter(start_date, end_date)
    order = _ORDER[metric]
    return conn.execute(
        f"""
        SELECT RANK() OVER (ORDER BY {order} DESC)::int AS rank,
               i.id AS item_id, i.name, i.category,
               SUM(s.units_sold)::bigint AS units_sold, SUM(s.revenue) AS revenue
        FROM sales s JOIN items i ON i.id = s.item_id{where}
        GROUP BY i.id, i.name, i.category
        ORDER BY {order} DESC, i.name
        LIMIT %(limit)s
        """, {**params, "limit": limit}).fetchall()


@router.get("/analytics/revenue-by-category", response_model=list[CategoryRevenue], summary="Revenue rollup by category")
def revenue_by_category(conn: Conn, start_date: date | None = None, end_date: date | None = None):
    where, params = _date_filter(start_date, end_date)
    return conn.execute(
        f"""
        SELECT i.category, COUNT(DISTINCT i.id)::int AS item_count,
               SUM(s.units_sold)::bigint AS units_sold, SUM(s.revenue) AS revenue,
               ROUND(100 * SUM(s.revenue) / NULLIF(SUM(SUM(s.revenue)) OVER (), 0), 2) AS revenue_share_pct
        FROM sales s JOIN items i ON i.id = s.item_id{where}
        GROUP BY i.category
        ORDER BY revenue DESC
        """, params).fetchall()


@router.get("/analytics/monthly-revenue", response_model=list[MonthlyRevenue],
            summary="Monthly revenue with month-over-month growth (SQL window function)")
def monthly_revenue(conn: Conn, category: Annotated[str | None, Query(max_length=60)] = None):
    return conn.execute(
        """
        WITH monthly AS (
            SELECT date_trunc('month', s.sale_date)::date AS month,
                   SUM(s.units_sold)::bigint AS units_sold, SUM(s.revenue) AS revenue
            FROM sales s JOIN items i ON i.id = s.item_id
            WHERE (%(category)s::text IS NULL OR i.category = %(category)s)
            GROUP BY 1
        )
        SELECT month, units_sold, revenue,
               ROUND(100 * (revenue - LAG(revenue) OVER w) / NULLIF(LAG(revenue) OVER w, 0), 2) AS mom_growth_pct
        FROM monthly WINDOW w AS (ORDER BY month)
        ORDER BY month
        """, {"category": category}).fetchall()


@router.get("/items", response_model=list[ItemOut], summary="List items (find an item_id for /forecast)")
def list_items(conn: Conn, category: Annotated[str | None, Query(max_length=60)] = None):
    return conn.execute(
        "SELECT id, name, category FROM items WHERE (%(c)s::text IS NULL OR category = %(c)s) ORDER BY name",
        {"c": category}).fetchall()


@router.get("/data-quality/latest", response_model=DataQualityRun, summary="What the latest ETL run rejected and repaired")
def latest_data_quality(conn: Conn):
    row = conn.execute(
        "SELECT id AS run_id, started_at, finished_at, source_file, status, rows_read, rows_rejected, rows_loaded, "
        "ROUND(100.0 * rows_rejected / NULLIF(rows_read, 0), 2)::float AS rejection_rate_pct, "
        "rejection_summary, repair_summary FROM etl_runs WHERE status = 'success' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise HTTPException(404, "no successful ETL run recorded yet")
    return row
