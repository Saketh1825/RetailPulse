"""VALIDATE + CLEAN: the core of the data-quality work.

Design
------
* Every row is checked by an ordered list of rules. A row that fails is NOT dropped silently: it is
  moved to the `rejected` frame with a machine-readable `reject_reason` and a human-readable
  `reject_detail`. The FIRST failing rule (in the order below) wins, so each rejected row has exactly
  one reason and the reason counts add up.
* Problems that are unambiguous and harmless to fix (case, whitespace, "$1,234.50", "12.0",
  DD/MM/YYYY dates) are REPAIRED, and counted in `repairs`, instead of rejected.
* Invariant (checked at the end): rows_read == rows_clean + rows_rejected. No row can vanish.

Rule order
----------
 1 missing_item_name      2 missing_date        3 invalid_date         4 date_out_of_range
 5 missing_units          6 invalid_units       7 negative_units       8 missing_revenue
 9 invalid_revenue       10 negative_revenue   11 zero_units_nonzero_revenue
12 units_outlier         13 price_outlier      (statistical: uses per-item medians of the rows above)
14 duplicate_row         15 conflicting_duplicate  (same item+day appears twice; FIRST occurrence is kept)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

log = logging.getLogger("etl.clean")

MISSING_TOKENS = frozenset({"", "null", "none", "nan"})   # NOTE: "N/A" is deliberately NOT here
MAX_INT32 = 2_147_483_647
MAX_NUMERIC_10_2 = 99_999_999.99       # largest value that fits NUMERIC(10,2)

REJECT_REASONS = [
    "missing_item_name", "missing_date", "invalid_date", "date_out_of_range",
    "missing_units", "invalid_units", "negative_units",
    "missing_revenue", "invalid_revenue", "negative_revenue",
    "zero_units_nonzero_revenue", "units_outlier", "price_outlier",
    "duplicate_row", "conflicting_duplicate",
]


@dataclass(frozen=True)
class Rules:
    min_date: date = date(2015, 1, 1)
    max_date: date = field(default_factory=date.today)      # future dates are invalid
    date_formats: tuple[str, ...] = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")   # slash/dash = DAY-first
    units_outlier_factor: float = 10.0
    units_outlier_floor: int = 50          # never call fewer than this an outlier (low-volume items)
    price_low: float = 0.25
    price_high: float = 4.0

    @classmethod
    def from_settings(cls, settings) -> Rules:
        return cls(
            min_date=date.fromisoformat(settings.etl_min_date),
            units_outlier_factor=settings.etl_units_outlier_factor,
            price_low=settings.etl_price_outlier_low,
            price_high=settings.etl_price_outlier_high,
        )


@dataclass
class CleanResult:
    clean: pd.DataFrame
    rejected: pd.DataFrame
    rejections: dict[str, int]
    repairs: dict[str, int]
    rows_read: int

    @property
    def rows_rejected(self) -> int:
        return len(self.rejected)


# --------------------------------------------------------------------------------------- parsers
def _collapse_ws(s: pd.Series) -> pd.Series:
    return s.str.replace(r"\s+", " ", regex=True).str.strip()


def _is_missing(s: pd.Series) -> pd.Series:
    return s.str.strip().str.lower().isin(MISSING_TOKENS)


def parse_dates(s: pd.Series, formats: tuple[str, ...]) -> tuple[pd.Series, pd.Series]:
    """Try each explicit format in order. Returns (parsed timestamps or NaT, format that matched)."""
    text = s.str.strip()
    parsed = pd.to_datetime(text, format=formats[0], errors="coerce")
    used = pd.Series(pd.NA, index=s.index, dtype=object)
    used[parsed.notna()] = formats[0]
    for fmt in formats[1:]:
        attempt = pd.to_datetime(text, format=fmt, errors="coerce")
        newly = parsed.isna() & attempt.notna()
        parsed = parsed.fillna(attempt)
        used[newly] = fmt
    return parsed, used


def parse_units(s: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """-> (numeric value, is_valid_integer_text, was_repaired). Accepts '12', '+12', '12.0', '1,234'."""
    t = s.str.strip()
    # NB: [0-9], not \d -- in Python \d also matches non-ASCII digits (e.g. Arabic-Indic), which pandas can't parse.
    plain = t.str.fullmatch(r"[+-]?[0-9]+")
    floaty = t.str.fullmatch(r"[+-]?[0-9]+\.0+")
    thousands = t.str.fullmatch(r"[+-]?[0-9]{1,3}(,[0-9]{3})+")
    ok = (plain | floaty | thousands).fillna(False).astype(bool)
    cleaned = t.where(ok).str.replace(",", "", regex=False).str.replace(r"\.0+$", "", regex=True)
    value = pd.to_numeric(cleaned, errors="coerce")
    ok = ok & value.notna()                                # safety net: unparseable is never "ok"
    repaired = ((floaty | thousands).fillna(False).astype(bool)) & ok
    return value, ok, repaired


def parse_money(s: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """-> (numeric value, is_valid, was_repaired). Strips currency symbols/spaces and thousands commas.
    A lone comma decimal ('12,5') is ambiguous, so it is NOT guessed: it is rejected as invalid."""
    t = s.str.strip()
    no_sym = t.str.replace(r"[$€£₹\s]", "", regex=True)
    thousands = no_sym.str.fullmatch(r"[+-]?[0-9]{1,3}(,[0-9]{3})+(\.[0-9]+)?").fillna(False).astype(bool)
    cleaned = no_sym.where(~thousands, no_sym.str.replace(",", "", regex=False))
    ok = cleaned.str.fullmatch(r"[+-]?[0-9]+(\.[0-9]+)?").fillna(False).astype(bool)
    value = pd.to_numeric(cleaned.where(ok), errors="coerce")
    ok = ok & value.notna() & value.map(lambda v: v == v and abs(v) != float("inf"))
    repaired = ok & (cleaned != t)
    return value, ok, repaired


# ------------------------------------------------------------------------------------- the rules
class _Flagger:
    """Assigns each row at most one rejection reason (first rule wins)."""

    def __init__(self, index: pd.Index):
        self.reason = pd.Series([None] * len(index), index=index, dtype=object)
        self.detail = pd.Series([None] * len(index), index=index, dtype=object)

    def flag(self, mask: pd.Series, reason: str, detail) -> None:
        m = mask.fillna(False).astype(bool) & self.reason.isna()
        if m.any():
            self.reason[m] = reason
            self.detail[m] = detail[m] if isinstance(detail, pd.Series) else detail


def clean_and_validate(raw: pd.DataFrame, rules: Rules | None = None) -> CleanResult:
    rules = rules or Rules()
    df = raw.copy()
    rows_read = len(df)
    fl = _Flagger(df.index)
    shown = lambda col: "raw value: " + df[col].map(repr)      # noqa: E731

    # ---- 1. item name ------------------------------------------------------------------------
    name_missing = _is_missing(df["item_name"])
    fl.flag(name_missing, "missing_item_name", "item_name is empty")

    # ---- 2-4. date ---------------------------------------------------------------------------
    date_missing = _is_missing(df["sale_date"])
    fl.flag(date_missing, "missing_date", "sale_date is empty")
    parsed, used_fmt = parse_dates(df["sale_date"], rules.date_formats)
    fl.flag(parsed.isna() & ~date_missing, "invalid_date",
            "not a real calendar date in an accepted format " + str(list(rules.date_formats)) + ": "
            + df["sale_date"].map(repr))
    in_range = (parsed >= pd.Timestamp(rules.min_date)) & (parsed <= pd.Timestamp(rules.max_date))
    fl.flag(parsed.notna() & ~in_range, "date_out_of_range",
            f"date outside [{rules.min_date}, {rules.max_date}]: " + df["sale_date"].map(repr))

    # ---- 5-7. units --------------------------------------------------------------------------
    units_missing = _is_missing(df["units_sold"])
    units, units_ok, units_repaired = parse_units(df["units_sold"])
    fl.flag(units_missing, "missing_units", "units_sold is empty")
    fl.flag(~units_ok & ~units_missing, "invalid_units", "units_sold is not a whole number: " + df["units_sold"].map(repr))
    fl.flag(units_ok & (units.abs() > MAX_INT32), "invalid_units", "units_sold exceeds INT range")
    fl.flag(units_ok & (units < 0), "negative_units", shown("units_sold"))

    # ---- 8-10. revenue -----------------------------------------------------------------------
    rev_missing = _is_missing(df["revenue"])
    revenue, rev_ok, rev_repaired = parse_money(df["revenue"])
    fl.flag(rev_missing, "missing_revenue", "revenue is empty")
    fl.flag(~rev_ok & ~rev_missing, "invalid_revenue", "revenue is not a monetary amount: " + df["revenue"].map(repr))
    fl.flag(rev_ok & (revenue > MAX_NUMERIC_10_2), "invalid_revenue", "revenue exceeds NUMERIC(10,2)")
    fl.flag(rev_ok & (revenue < 0), "negative_revenue", shown("revenue"))

    # ---- 11. cross-field consistency -----------------------------------------------------------
    fl.flag((units == 0) & (revenue > 0), "zero_units_nonzero_revenue",
            "units_sold is 0 but revenue is positive")

    # ---- 12-13. statistical outliers (medians come from rows that passed every rule above) ------
    df["item_key"] = _collapse_ws(df["item_name"]).str.casefold()
    df["_units"], df["_revenue"], df["_date"] = units, revenue.round(2), parsed
    valid = fl.reason.isna()
    med_units = df[valid].groupby("item_key")["_units"].median()
    limit = (med_units * rules.units_outlier_factor).clip(lower=rules.units_outlier_floor)
    row_limit = df["item_key"].map(limit)
    fl.flag(valid & (df["_units"] > row_limit), "units_outlier",
            "units_sold " + df["_units"].astype(str) + " is more than " + str(rules.units_outlier_factor)
            + "x this item's median (suspected entry error)")

    valid = fl.reason.isna() & (df["_units"] > 0)
    price = df["_revenue"] / df["_units"].where(df["_units"] > 0)
    med_price = price[valid].groupby(df.loc[valid, "item_key"]).median()
    row_med = df["item_key"].map(med_price)
    bad_price = valid & ((price < rules.price_low * row_med) | (price > rules.price_high * row_med))
    fl.flag(bad_price, "price_outlier",
            "implied unit price " + price.round(2).astype(str) + " vs item median " + row_med.round(2).astype(str))

    # ---- 14-15. duplicates on the business key (item, day); keep the FIRST occurrence ----------
    key = ["item_key", "_date_only"]
    df["_date_only"] = df["_date"].dt.date
    sub = df[fl.reason.isna()]
    is_dup = sub.duplicated(subset=key, keep="first")
    if is_dup.any():
        dups = sub[is_dup]
        merged = dups[key + ["_units", "_revenue"]].merge(
            sub[~is_dup][key + ["_units", "_revenue"]], on=key, how="left", suffixes=("", "_first"))
        same = pd.Series((merged["_units"].eq(merged["_units_first"])
                          & merged["_revenue"].eq(merged["_revenue_first"])).to_numpy(), index=dups.index)
        is_dup_all = pd.Series(False, index=df.index)
        is_dup_all[dups.index] = True
        same_all = pd.Series(False, index=df.index)
        same_all[dups.index] = same
        fl.flag(is_dup_all & same_all, "duplicate_row",
                "identical to an earlier row for the same item and date")
        fl.flag(is_dup_all & ~same_all, "conflicting_duplicate",
                "same item and date as an earlier row but different units/revenue; earlier row kept")

    # ---- split ---------------------------------------------------------------------------------
    rejected_mask = fl.reason.notna()
    rejected = raw[rejected_mask].copy()
    rejected["reject_reason"] = fl.reason[rejected_mask]
    rejected["reject_detail"] = fl.detail[rejected_mask]

    keep = df[~rejected_mask]
    clean = pd.DataFrame({
        "source_row": keep["source_row"],
        "item_key": keep["item_key"],
        "item_name_raw": keep["item_name"],
        "item_name": _collapse_ws(keep["item_name"]),
        "category_raw": keep["category"],
        "category": _collapse_ws(keep["category"]),
        "sale_date": keep["_date_only"],
        "units_sold": keep["_units"].astype("int64"),
        "revenue": keep["_revenue"].astype("float64"),
    })
    survived = ~rejected_mask
    repairs = {
        "date_format_dmy": int((survived & used_fmt.notna() & (used_fmt != rules.date_formats[0])).sum()),
        "money_symbols": int((survived & rev_repaired).sum()),
        "units_as_float": int((survived & units_repaired).sum()),
    }
    rejections = {r: int((rejected["reject_reason"] == r).sum()) for r in REJECT_REASONS}
    rejections = {k: v for k, v in rejections.items() if v}

    if len(clean) + len(rejected) != rows_read:                  # reconciliation invariant
        raise RuntimeError(f"row accounting broke: {rows_read} != {len(clean)} + {len(rejected)}")
    log.info("validated %d rows: %d clean, %d rejected, repairs=%s", rows_read, len(clean), len(rejected), repairs)
    return CleanResult(clean.reset_index(drop=True), rejected.reset_index(drop=True), rejections, repairs, rows_read)
