"""Unit tests for etl/clean.py, etl/transform.py and etl/extract.py -- no database needed.

Each rule is tested in isolation with a tiny hand-made frame, so a failure names the exact rule.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from etl.clean import REJECT_REASONS, Rules, clean_and_validate, parse_dates, parse_money, parse_units
from etl.extract import ExtractError, extract_csv
from etl.transform import transform

RULES = Rules(max_date=date(2025, 1, 1))     # fixed "today" so tests never depend on the calendar


def frame(rows: list[tuple]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["sale_date", "item_name", "category", "units_sold", "revenue"], dtype=str)
    df.insert(0, "source_row", range(2, len(df) + 2))
    return df


def baseline(n: int = 12, item: str = "Milk") -> list[tuple]:
    """n well-formed rows for one item (units 10, price 2.00) -> gives the outlier rules a median."""
    return [(f"2024-01-{d:02d}", item, "Dairy", "10", "20.00") for d in range(1, n + 1)]


def reasons(result) -> dict[str, str]:
    return dict(zip(result.rejected["source_row"], result.rejected["reject_reason"], strict=True))


# ----------------------------------------------------------------------------------- happy path
def test_valid_rows_pass_unchanged():
    res = clean_and_validate(frame(baseline()), RULES)
    assert len(res.clean) == 12 and res.rejected.empty and res.repairs == {
        "date_format_dmy": 0, "money_symbols": 0, "units_as_float": 0}
    assert res.clean.loc[0, "sale_date"] == date(2024, 1, 1)
    assert res.clean.loc[0, "units_sold"] == 10 and res.clean.loc[0, "revenue"] == 20.0


# --------------------------------------------------------------------------- one test per rule
@pytest.mark.parametrize("row, expected", [
    (("2024-02-01", "  ", "Dairy", "5", "10.00"), "missing_item_name"),
    (("", "Milk", "Dairy", "5", "10.00"), "missing_date"),
    (("2024-02-30", "Milk", "Dairy", "5", "10.00"), "invalid_date"),        # Feb 30 does not exist
    (("31/13/2024", "Milk", "Dairy", "5", "10.00"), "invalid_date"),        # month 13
    (("yesterday", "Milk", "Dairy", "5", "10.00"), "invalid_date"),
    (("N/A", "Milk", "Dairy", "5", "10.00"), "invalid_date"),               # N/A is invalid, not "missing"
    (("2031-01-01", "Milk", "Dairy", "5", "10.00"), "date_out_of_range"),   # in the future
    (("1999-01-01", "Milk", "Dairy", "5", "10.00"), "date_out_of_range"),   # before etl_min_date
    (("2024-02-01", "Milk", "Dairy", "", "10.00"), "missing_units"),
    (("2024-02-01", "Milk", "Dairy", "twelve", "10.00"), "invalid_units"),
    (("2024-02-01", "Milk", "Dairy", "3.5", "10.00"), "invalid_units"),     # fractional units
    (("2024-02-01", "Milk", "Dairy", "12 units", "10.00"), "invalid_units"),
    (("2024-02-01", "Milk", "Dairy", "-5", "10.00"), "negative_units"),
    (("2024-02-01", "Milk", "Dairy", "5", ""), "missing_revenue"),
    (("2024-02-01", "Milk", "Dairy", "5", "abc"), "invalid_revenue"),
    (("2024-02-01", "Milk", "Dairy", "5", "12.5.6"), "invalid_revenue"),
    (("2024-02-01", "Milk", "Dairy", "5", "12,5"), "invalid_revenue"),      # ambiguous comma: never guessed
    (("2024-02-01", "Milk", "Dairy", "5", "-10.00"), "negative_revenue"),
    (("2024-02-01", "Milk", "Dairy", "0", "10.00"), "zero_units_nonzero_revenue"),
    (("2024-02-01", "Milk", "Dairy", "9999", "20.00"), "units_outlier"),
    (("2024-02-01", "Milk", "Dairy", "10", "2000.00"), "price_outlier"),    # misplaced decimal
    (("2024-02-01", "Milk", "Dairy", "10", "0.50"), "price_outlier"),       # far too cheap
])
def test_rule_rejects_with_the_right_reason(row, expected):
    res = clean_and_validate(frame(baseline() + [row]), RULES)
    assert len(res.rejected) == 1, res.rejected
    assert res.rejected.iloc[0]["reject_reason"] == expected
    assert res.rejected.iloc[0]["reject_detail"]                         # every rejection explains itself
    assert len(res.clean) == 12


def test_zero_units_with_zero_revenue_is_valid():
    res = clean_and_validate(frame(baseline() + [("2024-02-01", "Milk", "Dairy", "0", "0.00")]), RULES)
    assert res.rejected.empty                                            # a genuine zero-sales day


def test_low_volume_items_are_not_flagged_as_outliers():
    rows = [(f"2024-01-{d:02d}", "Rare", "X", "0", "0.00") for d in range(1, 10)] + [
        ("2024-02-01", "Rare", "X", "4", "8.00")]                        # median is 0, but 4 is normal
    assert clean_and_validate(frame(rows), RULES).rejected.empty


# ------------------------------------------------------------------------------------ repairs
def test_repairs_are_applied_and_counted():
    rows = baseline(3) + [
        ("05/03/2024", "Milk", "Dairy", "10", "20.00"),          # day-first date            -> date_format_dmy
        ("2024-03-06", "Milk", "Dairy", "10.0", "$20.00"),        # float units + $ symbol   -> units_as_float, money_symbols
        ("2024-03-07", "Milk", "Dairy", "1,010", "2,020.00"),     # thousands separators     -> units_as_float, money_symbols
    ]
    res = clean_and_validate(frame(rows), Rules(max_date=date(2025, 1, 1), units_outlier_floor=5000))
    assert res.rejected.empty
    assert date(2024, 3, 5) in set(res.clean["sale_date"])       # 05/03/2024 means 5 March, not 3 May
    assert res.repairs == {"date_format_dmy": 1, "money_symbols": 2, "units_as_float": 2}
    assert res.clean["units_sold"].max() == 1010 and res.clean["revenue"].max() == 2020.0


def test_non_ascii_digits_are_rejected_not_crashed_on():
    """Regression: found by the fuzz test. Arabic-Indic digits matched \\d but could not be parsed."""
    res = clean_and_validate(frame(baseline() + [("2024-02-01", "Milk", "Dairy", "\u0663", "20.00"),
                                                 ("2024-02-02", "Milk", "Dairy", "5", "\u0662\u0660")]), RULES)
    assert sorted(res.rejected["reject_reason"]) == ["invalid_revenue", "invalid_units"]


def test_parsers_directly():
    v, ok, rep = parse_units(pd.Series(["12", "12.0", "1,234", "-3", "abc", "1.5", ""]))
    assert list(ok) == [True, True, True, True, False, False, False]
    assert list(rep) == [False, True, True, False, False, False, False]
    assert v.iloc[2] == 1234
    m, ok, rep = parse_money(pd.Series(["$1,234.50", "12", "€ 9.99", "12,5", "USD"]))
    assert list(ok) == [True, True, True, False, False] and m.iloc[0] == 1234.50
    d, used = parse_dates(pd.Series(["2024-03-05", "05/03/2024", "05-03-2024", "2024-13-01"]), RULES.date_formats)
    assert list(d.dt.day.iloc[:3]) == [5, 5, 5] and pd.isna(d.iloc[3])


# --------------------------------------------------------------------------------- duplicates
def test_exact_duplicate_is_rejected_and_first_kept():
    rows = baseline() + [("2024-01-05", "  MILK ", "dairy", "10", "20.00")]   # same key after normalising
    res = clean_and_validate(frame(rows), RULES)
    assert list(res.rejected["reject_reason"]) == ["duplicate_row"]
    assert len(res.clean) == 12


def test_conflicting_duplicate_keeps_first_occurrence():
    rows = baseline() + [("2024-01-05", "Milk", "Dairy", "14", "28.00")]
    res = clean_and_validate(frame(rows), RULES)
    assert list(res.rejected["reject_reason"]) == ["conflicting_duplicate"]
    kept = res.clean[res.clean["sale_date"] == date(2024, 1, 5)]
    assert len(kept) == 1 and kept.iloc[0]["units_sold"] == 10           # the earlier row won


def test_first_rule_wins_so_reasons_are_unique_and_add_up():
    rows = baseline() + [("garbage", "Milk", "Dairy", "abc", "xyz")]        # bad in three columns at once
    res = clean_and_validate(frame(rows), RULES)
    assert list(res.rejected["reject_reason"]) == ["invalid_date"]          # earliest rule in the order
    assert sum(res.rejections.values()) == len(res.rejected)


# ----------------------------------------------------------------------------- transformation
def test_transform_canonicalises_names_and_fills_categories():
    rows = baseline(10) + [
        ("2024-02-01", " milk ", "dairy", "10", "20.00"),
        ("2024-02-02", "MILK", "", "10", "20.00"),                       # blank category -> inherits
    ]
    res = clean_and_validate(frame(rows), RULES)
    out = transform(res.clean)
    assert list(out.items["name"]) == ["Milk"] and list(out.items["category"]) == ["Dairy"]
    assert out.repairs["item_name_case_whitespace"] == 2
    assert out.repairs["missing_category"] == 1
    assert out.repairs["category_case_whitespace"] == 1
    assert len(out.sales) == 12


def test_transform_reports_category_conflicts_instead_of_hiding_them():
    rows = baseline(10) + [("2024-02-01", "Milk", "Beverages", "10", "20.00")]
    out = transform(clean_and_validate(frame(rows), RULES).clean)
    assert out.repairs["category_conflict_rows"] == 1
    assert list(out.items["category"]) == ["Dairy"]                      # majority wins


# ----------------------------------------------------------------------------------- robustness
def test_fuzz_random_garbage_never_crashes_and_invariants_hold():
    import random
    rnd = random.Random(1234)
    pool = ["", " ", "abc", "-1", "0", "1", "12.5", "1e9", "NULL", "2024-01-01", "01/02/2024", "99/99/9999",
            "$5", "5,00", "9" * 30, "é", "  Milk ", "MILK", "Bread", "-0.0", "nan", "None", "٣"]
    rows = [tuple(rnd.choice(pool) for _ in range(5)) for _ in range(800)]
    res = clean_and_validate(frame(rows), RULES)
    assert len(res.clean) + len(res.rejected) == 800                     # nothing lost
    assert set(res.rejected["reject_reason"]) <= set(REJECT_REASONS)     # only known reasons
    assert res.clean[["sale_date", "item_name", "units_sold", "revenue"]].notna().all().all()
    assert (res.clean["units_sold"] >= 0).all() and (res.clean["revenue"] >= 0).all()
    assert (res.clean["item_name"].str.len() > 0).all()


# ------------------------------------------------------------------------------------- extract
def test_extract_rejects_unusable_files(tmp_path):
    with pytest.raises(ExtractError, match="not found"):
        extract_csv(tmp_path / "nope.csv")
    (tmp_path / "empty.csv").write_text("sale_date,item_name,category,units_sold,revenue\n")
    with pytest.raises(ExtractError, match="no data rows"):
        extract_csv(tmp_path / "empty.csv")
    (tmp_path / "bad.csv").write_text("date,name\n2024-01-01,x\n")
    with pytest.raises(ExtractError, match="missing required column"):
        extract_csv(tmp_path / "bad.csv")


def test_extract_reads_everything_as_text_and_tracks_line_numbers(tmp_path):
    (tmp_path / "ok.csv").write_text("Sale Date, Item Name,category,units_sold,revenue\n2024-01-01,Milk,Dairy,007,N/A\n")
    df = extract_csv(tmp_path / "ok.csv")
    assert df.loc[0, "units_sold"] == "007" and df.loc[0, "revenue"] == "N/A"   # no silent type guessing
    assert df.loc[0, "source_row"] == 2                                           # header is line 1
