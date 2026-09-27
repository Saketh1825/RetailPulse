"""Unit tests for app/nl_sql/validator.py. No database or network needed -- pure function tests.

This is the single most important test file in the NL-to-SQL feature: it is the adversarial suite
the project spec calls for (feed the validator deliberately malicious/invalid input and assert it
is rejected), plus the accept-path and LIMIT-rewriting behaviour.
"""
from __future__ import annotations

import pytest

from app.nl_sql.validator import validate_and_limit

MAX_ROWS = 50


# --------------------------------------------------------------------------------- accepted
@pytest.mark.parametrize("sql", [
    "SELECT * FROM items",
    "select * from items",  # case-insensitive
    "SELECT i.name, s.units_sold FROM items i JOIN sales s ON s.item_id = i.id",
    "SELECT * FROM items JOIN sales ON sales.item_id = items.id JOIN forecasts ON forecasts.item_id = items.id",
    "WITH recent AS (SELECT * FROM sales) SELECT * FROM recent",
    "WITH a AS (SELECT * FROM sales), b AS (SELECT * FROM a) SELECT * FROM b",
    "SELECT category, SUM(revenue) FROM sales JOIN items ON items.id = sales.item_id GROUP BY category",
    "SELECT * FROM items WHERE name = 'FROM sales'",  # string literal must not be parsed as SQL
    "SELECT * FROM public.items",  # explicit public schema is fine
    "  SELECT * FROM items  ",  # surrounding whitespace
    "SELECT * FROM items;",  # trailing semicolon
])
def test_accepts_safe_select(sql):
    result = validate_and_limit(sql, MAX_ROWS)
    assert result.ok, result.reject_reason
    assert result.sql is not None
    assert "LIMIT" in result.sql.upper()


def test_accepted_query_gets_a_limit_appended_when_missing():
    result = validate_and_limit("SELECT * FROM items", MAX_ROWS)
    assert result.ok
    assert result.limit_applied == MAX_ROWS
    assert f"LIMIT {MAX_ROWS + 1}" in result.sql
    assert any("no LIMIT clause" in n for n in result.notes)


def test_small_existing_limit_is_kept_and_probed():
    result = validate_and_limit("SELECT * FROM items LIMIT 5", MAX_ROWS)
    assert result.ok
    assert result.limit_applied == 5
    assert "LIMIT 6" in result.sql
    assert result.notes == []


def test_oversized_existing_limit_is_capped():
    result = validate_and_limit("SELECT * FROM items LIMIT 100000", MAX_ROWS)
    assert result.ok
    assert result.limit_applied == MAX_ROWS
    assert f"LIMIT {MAX_ROWS + 1}" in result.sql
    assert any("exceeds the cap" in n for n in result.notes)


# --------------------------------------------------------------------------------- rejected: not a SELECT
@pytest.mark.parametrize("sql,expected_reason", [
    ("DROP TABLE items", "not_a_select"),
    ("DELETE FROM sales", "not_a_select"),
    ("UPDATE items SET name = 'x'", "not_a_select"),
    ("INSERT INTO items (name) VALUES ('x')", "not_a_select"),
    ("TRUNCATE items", "not_a_select"),
    ("ALTER TABLE items ADD COLUMN x INT", "not_a_select"),
    ("CREATE TABLE evil (id INT)", "not_a_select"),
    ("GRANT ALL ON items TO PUBLIC", "not_a_select"),
])
def test_rejects_non_select_statements(sql, expected_reason):
    result = validate_and_limit(sql, MAX_ROWS)
    assert not result.ok
    assert result.reject_reason == expected_reason
    assert result.sql is None


# --------------------------------------------------------------------------------- rejected: multiple statements
@pytest.mark.parametrize("sql", [
    "SELECT 1; DROP TABLE items",
    "SELECT * FROM items; SELECT * FROM sales",
    "SELECT * FROM items;;",
])
def test_rejects_multiple_statements(sql):
    result = validate_and_limit(sql, MAX_ROWS)
    assert not result.ok
    assert result.reject_reason == "multiple_statements"


# --------------------------------------------------------------------------------- rejected: comments
@pytest.mark.parametrize("sql", [
    "SELECT * FROM items -- and then some mischief",
    "SELECT * FROM items /* block comment */",
    "SELECT 1; -- DROP TABLE items",
])
def test_rejects_comments(sql):
    result = validate_and_limit(sql, MAX_ROWS)
    assert not result.ok
    assert result.reject_reason in ("contains_comment", "multiple_statements")


# --------------------------------------------------------------------------------- rejected: whitelist
@pytest.mark.parametrize("sql", [
    "SELECT * FROM etl_runs",
    "SELECT * FROM nl_query_log",
    "SELECT * FROM forecast_models",
    "SELECT * FROM pg_catalog.pg_tables",
    "SELECT * FROM information_schema.columns",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM users",
    "SELECT * FROM items JOIN etl_runs ON 1=1",
])
def test_rejects_non_whitelisted_tables(sql):
    result = validate_and_limit(sql, MAX_ROWS)
    assert not result.ok
    assert result.reject_reason == "non_whitelisted_table"


def test_cte_name_does_not_need_to_be_whitelisted_but_its_source_does():
    ok = validate_and_limit("WITH x AS (SELECT * FROM sales) SELECT * FROM x", MAX_ROWS)
    assert ok.ok
    bad = validate_and_limit("WITH x AS (SELECT * FROM etl_runs) SELECT * FROM x", MAX_ROWS)
    assert not bad.ok
    assert bad.reject_reason == "non_whitelisted_table"


def test_subquery_in_from_with_comma_in_select_list_is_not_a_false_positive():
    # The comma is inside the subquery's own SELECT list, at paren depth 1, not a comma-join.
    result = validate_and_limit(
        "SELECT * FROM (SELECT name, category FROM items) sub", MAX_ROWS)
    assert result.ok


def test_comma_join_inside_a_join_condition_subquery_is_still_rejected():
    # A real comma-join nested inside a subquery must still be caught, at whatever depth it's at.
    result = validate_and_limit(
        "SELECT * FROM (SELECT * FROM items, sales) sub", MAX_ROWS)
    assert not result.ok
    assert result.reject_reason == "comma_join_not_supported"


# --------------------------------------------------------------------------------- rejected: dangerous functions
@pytest.mark.parametrize("sql", [
    "SELECT pg_sleep(10)",
    "SELECT pg_sleep(10), name FROM items",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT * FROM items WHERE pg_read_file('/etc/passwd') IS NOT NULL",
    "SELECT lo_import('/etc/passwd')",
    "SELECT dblink_connect('evil')",
    "SELECT setval('items_id_seq', 1)",
    "SELECT nextval('items_id_seq')",
    "COPY items TO '/tmp/x'",
])
def test_rejects_dangerous_functions(sql):
    result = validate_and_limit(sql, MAX_ROWS)
    assert not result.ok
    assert result.reject_reason in ("dangerous_function", "not_a_select")


# --------------------------------------------------------------------------------- rejected: misc structural
def test_rejects_select_into():
    result = validate_and_limit("SELECT * INTO new_table FROM items", MAX_ROWS)
    assert not result.ok
    assert result.reject_reason == "select_into"


def test_rejects_comma_join():
    result = validate_and_limit("SELECT * FROM items, sales WHERE items.id = sales.item_id", MAX_ROWS)
    assert not result.ok
    assert result.reject_reason == "comma_join_not_supported"


def test_rejects_offset():
    result = validate_and_limit("SELECT * FROM items LIMIT 10 OFFSET 5", MAX_ROWS)
    assert not result.ok
    assert result.reject_reason == "offset_not_supported"


# --------------------------------------------------------------------------------- rejected: degenerate input
@pytest.mark.parametrize("sql", ["", "   ", "\n\t"])
def test_rejects_empty_input(sql):
    result = validate_and_limit(sql, MAX_ROWS)
    assert not result.ok
    assert result.reject_reason == "empty_query"


def test_rejects_gibberish():
    result = validate_and_limit("asdkjaslkdj alksjdlkasjd", MAX_ROWS)
    assert not result.ok
    assert result.reject_reason == "not_a_select"


def test_rejects_oversized_sql():
    huge = "SELECT * FROM items WHERE name = '" + ("x" * 5000) + "'"
    result = validate_and_limit(huge, MAX_ROWS)
    assert not result.ok
    assert result.reject_reason == "sql_too_long"


def test_validator_never_raises_on_arbitrary_input():
    # A grab-bag of odd inputs a hostile or confused LLM might produce. Nothing here should raise.
    for sql in [None, 123, object(), "'''''''", "SELECT * FROM (((((", "\x00\x01SELECT",
                "SELECT " + "(" * 500]:
        try:
            result = validate_and_limit(sql, MAX_ROWS)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"validator raised on input {sql!r}: {exc!r}")
        assert result.ok is False or result.sql is not None
