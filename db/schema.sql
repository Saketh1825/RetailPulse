-- RetailPulse schema (PostgreSQL 14+). Idempotent: safe to run more than once.
-- Design notes live in docs/DATABASE.md.

-- Dimension: one row per product.
CREATE TABLE IF NOT EXISTS items (
    id        SERIAL PRIMARY KEY,
    name      VARCHAR(120) NOT NULL,
    category  VARCHAR(60)  NOT NULL,
    CONSTRAINT uq_items_name UNIQUE (name)
);
CREATE INDEX IF NOT EXISTS idx_items_category ON items (category);

-- Fact: one row per item per day (daily grain).
CREATE TABLE IF NOT EXISTS sales (
    id          BIGSERIAL PRIMARY KEY,
    item_id     INT           NOT NULL REFERENCES items (id) ON DELETE RESTRICT,
    sale_date   DATE          NOT NULL,
    units_sold  INT           NOT NULL,
    revenue     NUMERIC(10,2) NOT NULL,
    CONSTRAINT ck_sales_units_nonneg   CHECK (units_sold >= 0),
    CONSTRAINT ck_sales_revenue_nonneg CHECK (revenue >= 0),
    -- Doubles as the (item_id, sale_date) composite index from the approved spec, AND makes the
    -- loader idempotent (re-running the ETL updates rows instead of duplicating them).
    CONSTRAINT uq_sales_item_date UNIQUE (item_id, sale_date)
);
-- Serves date-range scans across all items (revenue-by-category, monthly trend).
CREATE INDEX IF NOT EXISTS idx_sales_date ON sales (sale_date);

-- Batch-generated demand forecasts (written by app/forecasting/predict.py).
CREATE TABLE IF NOT EXISTS forecasts (
    id               SERIAL PRIMARY KEY,
    item_id          INT              NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    forecast_date    DATE             NOT NULL,
    predicted_units  DOUBLE PRECISION NOT NULL CHECK (predicted_units >= 0),
    generated_at     TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CONSTRAINT uq_forecasts_item_date UNIQUE (item_id, forecast_date)
);

-- Held-out evaluation numbers per item, so the API can show model quality next to each forecast.
CREATE TABLE IF NOT EXISTS forecast_models (
    item_id                       INT PRIMARY KEY REFERENCES items (id) ON DELETE CASCADE,
    trained_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    model_type                    TEXT        NOT NULL,
    train_start                   DATE,
    train_end                     DATE,
    test_start                    DATE,
    test_end                      DATE,
    n_train_rows                  INT,
    test_mae                      DOUBLE PRECISION,
    test_wape                     DOUBLE PRECISION,
    baseline_seasonal_naive_mae   DOUBLE PRECISION,
    baseline_mean_mae             DOUBLE PRECISION,
    features                      JSONB,
    coefficients                  JSONB
);

-- Audit trail of every ETL run: what was read, loaded, rejected and why.
CREATE TABLE IF NOT EXISTS etl_runs (
    id                 SERIAL PRIMARY KEY,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ,
    source_file        TEXT        NOT NULL,
    status             TEXT        NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    rows_read          INT,
    rows_rejected      INT,
    rows_loaded        INT,
    items_loaded       INT,
    rejection_summary  JSONB,
    repair_summary     JSONB,
    rejected_file      TEXT,
    error_message      TEXT
);

-- Audit trail of natural-language queries (written by the normal read/write connection;
-- the read-only role has no access to this table).
CREATE TABLE IF NOT EXISTS nl_query_log (
    id             BIGSERIAL PRIMARY KEY,
    asked_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    question       TEXT        NOT NULL,
    generated_sql  TEXT,
    executed_sql   TEXT,
    status         TEXT        NOT NULL,
    reject_reason  TEXT,
    row_count      INT,
    llm_ms         INT,
    db_ms          INT,
    total_ms       INT
);
