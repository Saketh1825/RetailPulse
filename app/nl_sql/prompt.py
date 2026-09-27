"""Build the prompt sent to the LLM to translate a question into SQL.

This is layer 1 of 4 in the NL-to-SQL defense (see docs/NL_SQL_SECURITY.md): it reduces how often
the model tries something unsafe, but it is NOT a security boundary by itself -- the validator
(app/nl_sql/validator.py) and the read-only database role do not trust anything about how the SQL
was produced. Deliberately, the schema shown here covers only the three tables the read-only role
can actually reach (items, sales, forecasts); the audit tables (etl_runs, forecast_models,
nl_query_log) are never mentioned, so the model has no reason to try to reach them.
"""
from __future__ import annotations

SCHEMA = """\
items (
  id       INTEGER PRIMARY KEY,
  name     VARCHAR(120),   -- product name, e.g. 'Sourdough Loaf'
  category VARCHAR(60)     -- e.g. 'Bakery', 'Dairy'
)

sales (
  id         BIGINT PRIMARY KEY,
  item_id    INTEGER REFERENCES items(id),
  sale_date  DATE,
  units_sold INTEGER,
  revenue    NUMERIC(10,2)
)
-- one row per item per day; a missing (item_id, sale_date) pair means zero sales that day

forecasts (
  id              INTEGER PRIMARY KEY,
  item_id         INTEGER REFERENCES items(id),
  forecast_date   DATE,
  predicted_units DOUBLE PRECISION,
  generated_at    TIMESTAMPTZ
)
"""

RULES = """\
You translate a retail-analytics question into exactly one PostgreSQL SELECT statement.

Rules, all mandatory:
1. Output ONLY the SQL. No explanation, no markdown code fences, no semicolon at the end.
2. Exactly one statement. A single SELECT, or a WITH ... SELECT using common table expressions.
3. Use ONLY the tables in the schema above (items, sales, forecasts). Never reference any other
   table, view, or a schema-qualified name like pg_catalog.* or information_schema.*.
4. Never use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, GRANT, COPY, or any
   data-modifying or administrative statement or function.
5. No comments (-- or /* */).
6. Prefer explicit JOIN ... ON over comma-separated tables in FROM.
7. Always include a LIMIT clause bounding the result to a reasonable number of rows (e.g. 100).
8. If the question cannot be answered from this schema, output exactly: SELECT NULL WHERE FALSE
"""

FEW_SHOT: list[tuple[str, str]] = [
    ("Which 5 items had the highest revenue in 2023?",
     "SELECT i.name, SUM(s.revenue) AS revenue FROM items i JOIN sales s ON s.item_id = i.id "
     "WHERE s.sale_date >= '2023-01-01' AND s.sale_date <= '2023-12-31' "
     "GROUP BY i.name ORDER BY revenue DESC LIMIT 5"),
    ("What is the forecasted demand for Sourdough Loaf next month?",
     "SELECT f.forecast_date, f.predicted_units FROM forecasts f JOIN items i ON i.id = f.item_id "
     "WHERE i.name = 'Sourdough Loaf' ORDER BY f.forecast_date LIMIT 100"),
    ("How many products are in the Dairy category?",
     "SELECT COUNT(*) FROM items WHERE category = 'Dairy' LIMIT 1"),
]


def build_messages(question: str) -> list[dict[str, str]]:
    """Return an OpenAI-style chat 'messages' list: one system message with schema + rules +
    few-shot examples, then the user's question."""
    system = f"{RULES}\nSchema:\n{SCHEMA}"
    messages = [{"role": "system", "content": system}]
    for q, sql in FEW_SHOT:
        messages.append({"role": "user", "content": q})
        messages.append({"role": "assistant", "content": sql})
    messages.append({"role": "user", "content": question})
    return messages
