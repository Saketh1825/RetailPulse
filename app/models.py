"""Pydantic request/response models -- the API contract (also what Swagger UI renders)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Health(BaseModel):
    status: str
    database: str
    readonly_database: str
    llm_configured: bool


# ------------------------------------------------------------------------------------ analytics
class Summary(BaseModel):
    items: int
    sales_rows: int
    first_date: date | None
    last_date: date | None
    total_units: int
    total_revenue: float


class TopItem(BaseModel):
    rank: int
    item_id: int
    name: str
    category: str
    units_sold: int
    revenue: float


class CategoryRevenue(BaseModel):
    category: str
    item_count: int
    units_sold: int
    revenue: float
    revenue_share_pct: float


class MonthlyRevenue(BaseModel):
    month: date
    units_sold: int
    revenue: float
    mom_growth_pct: float | None = Field(None, description="Change vs previous month; null for the first month")


class ItemOut(BaseModel):
    id: int
    name: str
    category: str


class ItemSalesPoint(BaseModel):
    sale_date: date
    units_sold: int
    revenue: float


class DataQualityRun(BaseModel):
    run_id: int
    started_at: datetime
    finished_at: datetime | None
    source_file: str
    status: str
    rows_read: int | None
    rows_rejected: int | None
    rows_loaded: int | None
    rejection_rate_pct: float | None
    rejection_summary: dict[str, int] | None
    repair_summary: dict[str, int] | None


# ------------------------------------------------------------------------------------ forecast
class ModelInfo(BaseModel):
    model_type: str
    trained_at: datetime
    train_period: str
    holdout_period: str
    holdout_mae_units_per_day: float | None
    holdout_wape_pct: float | None
    baseline_seasonal_naive_mae: float | None
    baseline_mean_mae: float | None
    beats_seasonal_naive: bool | None


class ForecastPoint(BaseModel):
    date: date
    predicted_units: float


class ForecastResponse(BaseModel):
    item_id: int
    item_name: str
    category: str
    horizon_days: int
    generated_at: datetime
    model: ModelInfo | None
    forecast: list[ForecastPoint]
    caveat: str


# ------------------------------------------------------------------------------- NL-to-SQL
class QueryRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    question: str = Field(..., min_length=3, max_length=2000,
                          examples=["Which 5 items had the highest revenue in 2023?"])


class QueryTimings(BaseModel):
    llm_ms: int
    validate_ms: int
    db_ms: int
    total_ms: int


class QueryResponse(BaseModel):
    question: str
    sql: str = Field(description="The exact SQL that was executed (after validation and LIMIT enforcement)")
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = Field(description="True if more rows existed than the row cap allows")
    limit_applied: int
    timings: QueryTimings
    notes: list[str] = Field(default_factory=list, description="What the validator changed, if anything")


class ValidateRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=10000, examples=["SELECT name FROM items LIMIT 5"])


class ValidateResponse(BaseModel):
    ok: bool
    reject_reason: str | None = Field(None, description="Machine-readable rule that rejected the SQL")
    detail: str | None = Field(None, description="Human-readable explanation of the rejection")
    rewritten_sql: str | None = Field(None, description="The SQL that WOULD run (LIMIT enforced); never executed here")
    limit_applied: int | None = None
    notes: list[str] = Field(default_factory=list)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
