"""ETL end-to-end against a real PostgreSQL, using the generator's ground-truth manifest.

The strongest check here: the generator KNOWS how many rows of each problem it injected, so we can assert
that the ETL found exactly those -- not merely "some" -- and that the values loaded are the true ones.
"""
from __future__ import annotations

import pandas as pd
import psycopg
import pytest

from etl.pipeline import run_etl

pytestmark = pytest.mark.integration


def test_rejections_match_injected_problems_exactly(loaded_db):
    report, manifest = loaded_db["report"], loaded_db["manifest"]
    assert report.rejections == {k: v for k, v in manifest["injected_rejections"].items() if v}
    assert report.rows_rejected == manifest["expected_rows_rejected"]
    assert report.rows_loaded == manifest["expected_rows_loaded"]


def test_repairs_match_injected_problems_exactly(loaded_db):
    repairs, injected = loaded_db["report"].repairs, loaded_db["manifest"]["injected_repairs"]
    for key, expected in injected.items():
        assert repairs[key] == expected, key


def test_row_accounting_reconciles(loaded_db):
    r = loaded_db["report"]
    assert r.rows_read == r.rows_loaded + r.rows_rejected == loaded_db["manifest"]["total_rows_written"]


def test_loaded_values_are_the_true_values(loaded_db):
    """Every row in the database equals the clean ground truth; the only missing rows are the rejected ones."""
    truth = loaded_db["clean"].copy()
    truth["sale_date"] = truth["sale_date"].dt.date
    with psycopg.connect(loaded_db["dsn"]) as conn:
        db = pd.DataFrame(conn.execute(
            "SELECT i.name AS item_name, s.sale_date, s.units_sold, s.revenue FROM sales s "
            "JOIN items i ON i.id = s.item_id").fetchall(),
            columns=["item_name", "sale_date", "units_sold", "revenue"])
    db["revenue"] = db["revenue"].astype(float)
    merged = db.merge(truth, on=["item_name", "sale_date"], how="left", suffixes=("", "_true"))
    assert merged["units_sold_true"].notna().all(), "loaded a row that is not in the ground truth"
    assert (merged["units_sold"] == merged["units_sold_true"]).all()
    assert ((merged["revenue"] - merged["revenue_true"]).abs() < 0.005).all()
    assert len(truth) - len(db) == sum(
        v for k, v in loaded_db["manifest"]["injected_rejections"].items() if not k.endswith("duplicate_row")
        and k != "conflicting_duplicate")


def test_items_are_normalised_no_case_or_whitespace_variants(loaded_db):
    with psycopg.connect(loaded_db["dsn"]) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM items")]
        cats = [r[0] for r in conn.execute("SELECT DISTINCT category FROM items")]
    assert len(names) == 24 and len({n.casefold() for n in names}) == 24
    assert sorted(cats) == ["Bakery", "Beverages", "Dairy", "Household", "Personal Care", "Snacks"]
    assert all(n == " ".join(n.split()) for n in names)


def test_rerunning_the_same_file_is_idempotent(loaded_db, tmp_path):
    with psycopg.connect(loaded_db["dsn"]) as conn:
        before = conn.execute("SELECT count(*), sum(revenue) FROM sales").fetchone()
    again = run_etl(loaded_db["csv"], dsn=loaded_db["dsn"], rejected_dir=tmp_path)
    with psycopg.connect(loaded_db["dsn"]) as conn:
        after = conn.execute("SELECT count(*), sum(revenue) FROM sales").fetchone()
    assert before == after
    assert (again.load_stats.sales_inserted, again.load_stats.sales_updated) == (0, 0)
    assert again.load_stats.sales_unchanged == loaded_db["report"].rows_loaded


def test_rejected_rows_are_saved_with_reasons(loaded_db):
    df = pd.read_csv(loaded_db["report"].rejected_file, dtype=str, keep_default_na=False)
    assert len(df) == loaded_db["report"].rows_rejected
    assert {"source_row", "reject_reason", "reject_detail"} <= set(df.columns)
    assert (df["reject_reason"] != "").all() and (df["reject_detail"] != "").all()


def test_every_run_is_audited(loaded_db):
    with psycopg.connect(loaded_db["dsn"]) as conn:
        row = conn.execute("SELECT status, rows_read, rows_rejected, rejection_summary FROM etl_runs "
                           "ORDER BY id LIMIT 1").fetchone()
    assert row[0] == "success" and row[1] == loaded_db["report"].rows_read
    assert row[3] == loaded_db["report"].rejections


def test_dry_run_writes_nothing(loaded_db, tmp_path):
    with psycopg.connect(loaded_db["dsn"]) as conn:
        runs_before = conn.execute("SELECT count(*) FROM etl_runs").fetchone()[0]
    report = run_etl(loaded_db["csv"], dsn=loaded_db["dsn"], dry_run=True, rejected_dir=tmp_path)
    assert report.rejected_file is None and not list(tmp_path.iterdir())
    with psycopg.connect(loaded_db["dsn"]) as conn:
        assert conn.execute("SELECT count(*) FROM etl_runs").fetchone()[0] == runs_before


def test_changed_source_values_update_existing_rows(loaded_db, tmp_path):
    csv = tmp_path / "one_day.csv"
    with psycopg.connect(loaded_db["dsn"]) as conn:
        item, day, units, rev = conn.execute(
            "SELECT i.name, s.sale_date, s.units_sold, s.revenue FROM sales s JOIN items i ON i.id=s.item_id "
            "WHERE s.units_sold BETWEEN 5 AND 20 ORDER BY s.id LIMIT 1").fetchone()
    csv.write_text(f"sale_date,item_name,category,units_sold,revenue\n{day},{item},Snacks,{units + 1},{rev}\n")
    rep = run_etl(csv, dsn=loaded_db["dsn"], rejected_dir=tmp_path)
    assert rep.load_stats.sales_updated == 1
    with psycopg.connect(loaded_db["dsn"]) as conn:                # restore, so other tests are unaffected
        conn.execute("UPDATE sales SET units_sold = %s WHERE sale_date = %s AND item_id = "
                     "(SELECT id FROM items WHERE name = %s)", (units, day, item))
        conn.commit()


# --------------------------------------------------- the database itself refuses bad data
@pytest.mark.parametrize("stmt", [
    "INSERT INTO sales (item_id, sale_date, units_sold, revenue) VALUES ((SELECT min(id) FROM items), '2030-01-01', -1, 1)",
    "INSERT INTO sales (item_id, sale_date, units_sold, revenue) VALUES ((SELECT min(id) FROM items), '2030-01-01', 1, -1)",
    "INSERT INTO sales (item_id, sale_date, units_sold, revenue) VALUES (999999, '2030-01-01', 1, 1)",        # FK
    "INSERT INTO sales (item_id, sale_date, units_sold, revenue) SELECT item_id, sale_date, 1, 1 FROM sales LIMIT 1",  # UNIQUE
    "INSERT INTO sales (item_id, sale_date, units_sold, revenue) VALUES ((SELECT min(id) FROM items), NULL, 1, 1)",   # NOT NULL
    "INSERT INTO items (name, category) SELECT name, 'X' FROM items LIMIT 1",                                  # UNIQUE name
])
def test_database_constraints_reject_bad_rows(loaded_db, stmt):
    with psycopg.connect(loaded_db["dsn"]) as conn, pytest.raises(psycopg.errors.IntegrityError):
        conn.execute(stmt)
