"""Generate a realistic SYNTHETIC retail sales file with deliberate data-quality problems.

Why synthetic?  It is reproducible (fixed seed), needs no download/licence, and -- most importantly --
lets us write a *manifest* of exactly which problems were injected, so the ETL tests can assert
"injected problems == detected problems".  To use a real Kaggle dataset instead, see data/README.md.

Grain: one row per item per day.  Columns: sale_date, item_name, category, units_sold, revenue.

    python -m scripts.generate_sample_data                 # writes data/raw/sales_raw.csv (+ manifest)
    python -m scripts.generate_sample_data --seed 7 --out /tmp/x.csv
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import PROJECT_ROOT

WEEK = {  # Monday..Sunday multipliers
    "grocery": [0.85, 0.85, 0.90, 0.95, 1.10, 1.35, 1.20],
    "household": [0.80, 0.80, 0.85, 0.90, 1.05, 1.60, 1.00],
    "care": [1.05, 1.05, 1.05, 1.05, 1.00, 0.90, 0.85],
}


@dataclass(frozen=True)
class ItemSpec:
    name: str
    category: str
    base_units: float      # average units/day before seasonality
    price: float           # list price at start of series
    week: str
    season_amp: float      # annual seasonality amplitude (0.3 = +/-30%)
    season_peak_doy: int   # day-of-year at which the seasonal peak occurs
    trend_per_year: float  # e.g. 0.06 = +6% volume per year
    start: str | None = None   # launched later than the series start
    end: str | None = None     # discontinued


ITEMS = [
    ItemSpec("Cold Brew Coffee 250ml", "Beverages", 38, 3.20, "grocery", 0.35, 200, 0.08),
    ItemSpec("Orange Juice 1L", "Beverages", 55, 2.80, "grocery", 0.15, 190, 0.02),
    ItemSpec("Sparkling Water 500ml", "Beverages", 90, 1.10, "grocery", 0.30, 200, 0.05),
    ItemSpec("Green Tea Bags 25ct", "Beverages", 14, 2.60, "care", 0.25, 15, 0.04),
    ItemSpec("Whole Milk 1L", "Dairy", 140, 1.25, "grocery", 0.04, 200, 0.00),
    ItemSpec("Greek Yogurt 500g", "Dairy", 48, 3.10, "grocery", 0.10, 190, 0.07),
    ItemSpec("Cheddar Cheese 200g", "Dairy", 27, 3.90, "grocery", 0.08, 355, 0.01),
    ItemSpec("Salted Butter 250g", "Dairy", 22, 3.60, "grocery", 0.12, 350, 0.00),
    ItemSpec("Sourdough Loaf", "Bakery", 42, 4.20, "grocery", 0.05, 100, 0.06),
    ItemSpec("Butter Croissant", "Bakery", 65, 1.90, "grocery", 0.05, 100, 0.03),
    ItemSpec("Multigrain Bread", "Bakery", 75, 2.40, "grocery", 0.03, 100, -0.02),
    ItemSpec("Blueberry Muffin", "Bakery", 30, 2.20, "grocery", 0.10, 60, 0.05, start="2022-09-01"),
    ItemSpec("Potato Chips 150g", "Snacks", 80, 2.10, "grocery", 0.10, 200, 0.02),
    ItemSpec("Salted Peanuts 200g", "Snacks", 20, 2.70, "grocery", 0.06, 340, 0.01),
    ItemSpec("Dark Chocolate Bar", "Snacks", 34, 2.50, "grocery", 0.20, 355, 0.05),
    ItemSpec("Granola Bar 6pk", "Snacks", 12, 4.30, "care", 0.05, 10, 0.09),
    ItemSpec("Dish Soap 500ml", "Household", 26, 2.30, "household", 0.02, 100, 0.00),
    ItemSpec("Paper Towels 2pk", "Household", 31, 3.40, "household", 0.03, 340, 0.01),
    ItemSpec("Laundry Detergent 2L", "Household", 9, 9.80, "household", 0.03, 100, -0.01),
    ItemSpec("Trash Bags 30ct", "Household", 18, 4.60, "household", 0.02, 100, 0.00, end="2024-09-30"),
    ItemSpec("Shampoo 400ml", "Personal Care", 16, 5.90, "care", 0.04, 100, 0.02),
    ItemSpec("Toothpaste 100ml", "Personal Care", 24, 3.10, "care", 0.02, 100, 0.00),
    ItemSpec("Hand Wash 250ml", "Personal Care", 28, 2.20, "care", 0.10, 15, 0.03),
    ItemSpec("Body Lotion 200ml", "Personal Care", 5, 6.40, "care", 0.30, 15, 0.04),
]


def generate_clean(start: str, end: str, seed: int) -> pd.DataFrame:
    """Simulate clean daily sales: base level x weekday x season x trend x holiday x promo + Poisson noise."""
    rng = np.random.default_rng(seed)
    days = pd.date_range(start, end, freq="D")
    doy = days.dayofyear.to_numpy()
    dow = days.dayofweek.to_numpy()
    years = (days - days[0]).days.to_numpy() / 365.25
    frames = []
    for spec in ITEMS:
        week = np.array(WEEK[spec.week])[dow]
        season = 1 + spec.season_amp * np.cos(2 * np.pi * (doy - spec.season_peak_doy) / 365.25)
        trend = 1 + spec.trend_per_year * years
        holiday = np.ones(len(days))
        if spec.category in {"Snacks", "Bakery", "Dairy", "Beverages"}:
            holiday[(days.month == 12) & (days.day >= 18) & (days.day <= 24)] = 1.30
        holiday[(days.month == 1) & (days.day <= 2)] = 0.80
        promo = rng.random(len(days)) < 0.04
        promo_lift = np.where(promo, rng.uniform(1.4, 1.8, len(days)), 1.0)
        lam = spec.base_units * week * season * trend * holiday * promo_lift
        units = rng.poisson(lam)
        price = spec.price * (1 + 0.03 * years) * np.where(promo, rng.uniform(0.85, 0.90, len(days)), 1.0)
        df = pd.DataFrame({
            "sale_date": days, "item_name": spec.name, "category": spec.category,
            "units_sold": units, "unit_price": price,
        })
        if spec.name == "Sparkling Water 500ml":            # simulated stock-out: zero sales, 10 days
            out = (df.sale_date >= "2023-07-10") & (df.sale_date <= "2023-07-19")
            df.loc[out, "units_sold"] = 0
        if spec.start:
            df = df[df.sale_date >= spec.start]
        if spec.end:
            df = df[df.sale_date <= spec.end]
        frames.append(df)
    out = pd.concat(frames, ignore_index=True).sort_values(["sale_date", "item_name"], ignore_index=True)
    out["revenue"] = (out["units_sold"] * out["unit_price"]).round(2)
    return out[["sale_date", "item_name", "category", "units_sold", "revenue"]]


# Rates are fractions of base rows. Each row receives at most ONE corruption (disjoint sets).
REJECT_RATES = {
    "missing_item_name": 0.0020, "missing_date": 0.0020, "invalid_date": 0.0030,
    "date_out_of_range": 0.0010, "missing_units": 0.0025, "invalid_units": 0.0025,
    "negative_units": 0.0015, "units_outlier": 0.0010, "missing_revenue": 0.0025,
    "invalid_revenue": 0.0020, "negative_revenue": 0.0010, "price_outlier": 0.0010,
    "zero_units_nonzero_revenue": 0.0010,
}
REPAIR_RATES = {
    "date_format_dmy": 0.030, "money_symbols": 0.020, "units_as_float": 0.010,
    "item_name_case_whitespace": 0.050, "category_case_whitespace": 0.040, "missing_category": 0.005,
}
DUPLICATE_RATES = {"duplicate_row": 0.015, "conflicting_duplicate": 0.003}


def _messy_text(value: str, rng: np.random.Generator) -> str:
    """Return a case/whitespace variant that is guaranteed to DIFFER from the input."""
    variants = [value.upper(), value.lower(), f"  {value}", f"{value}  ", value.replace(" ", "  ")]
    variants = [v for v in variants if v != value]
    return variants[int(rng.integers(len(variants)))]


def inject_dirt(clean: pd.DataFrame, seed: int) -> tuple[pd.DataFrame, dict]:
    rng = np.random.default_rng(seed + 1)
    df = clean.copy()
    df["sale_date"] = df["sale_date"].dt.strftime("%Y-%m-%d")
    df["units_sold"] = df["units_sold"].astype(str)
    df["revenue"] = df["revenue"].map(lambda v: f"{v:.2f}")
    n = len(df)
    perm = rng.permutation(n).tolist()
    cursor = 0
    injected: dict[str, int] = {}

    def take(kind: str, rate: float, require=None) -> np.ndarray:
        """Carve the next disjoint slice of rows for this corruption (optionally filtered)."""
        nonlocal cursor
        k = int(round(n * rate))
        picked = []
        while len(picked) < k:
            i = perm[cursor]
            cursor += 1
            if require is None or require(i):
                picked.append(i)
        injected[kind] = k
        return np.array(picked, dtype=int)

    units_int = clean["units_sold"].to_numpy()
    positive = lambda i: units_int[i] > 0          # noqa: E731

    idx = take("missing_item_name", REJECT_RATES["missing_item_name"]); df.loc[idx, "item_name"] = ""
    idx = take("missing_date", REJECT_RATES["missing_date"]); df.loc[idx, "sale_date"] = ""
    idx = take("invalid_date", REJECT_RATES["invalid_date"])
    bad_dates = ["2023-02-30", "31/13/2023", "yesterday", "N/A", "2023-99-01", "13-13-2023"]
    df.loc[idx, "sale_date"] = [bad_dates[i % len(bad_dates)] for i in range(len(idx))]
    idx = take("date_out_of_range", REJECT_RATES["date_out_of_range"])
    df.loc[idx, "sale_date"] = [["2031-06-15", "1999-01-01"][i % 2] for i in range(len(idx))]
    idx = take("missing_units", REJECT_RATES["missing_units"]); df.loc[idx, "units_sold"] = ""
    idx = take("invalid_units", REJECT_RATES["invalid_units"])
    bad_units = ["twelve", "abc", "12 units", "3.5", "--", "1e3x"]
    df.loc[idx, "units_sold"] = [bad_units[i % len(bad_units)] for i in range(len(idx))]
    idx = take("negative_units", REJECT_RATES["negative_units"], positive)
    df.loc[idx, "units_sold"] = "-5"
    idx = take("units_outlier", REJECT_RATES["units_outlier"], positive)
    df.loc[idx, "units_sold"] = "99999"
    idx = take("missing_revenue", REJECT_RATES["missing_revenue"]); df.loc[idx, "revenue"] = ""
    idx = take("invalid_revenue", REJECT_RATES["invalid_revenue"])
    bad_rev = ["abc", "12.5.6", "N/A", "USD"]
    df.loc[idx, "revenue"] = [bad_rev[i % len(bad_rev)] for i in range(len(idx))]
    idx = take("negative_revenue", REJECT_RATES["negative_revenue"], positive)
    df.loc[idx, "revenue"] = ["-" + df.at[i, "revenue"] for i in idx]
    idx = take("price_outlier", REJECT_RATES["price_outlier"], positive)          # misplaced decimal
    df.loc[idx, "revenue"] = [f"{float(df.at[i, 'revenue']) * 50:.2f}" for i in idx]
    idx = take("zero_units_nonzero_revenue", REJECT_RATES["zero_units_nonzero_revenue"], positive)
    df.loc[idx, "units_sold"] = "0"

    # ---- repairable problems: the row must SURVIVE the ETL with a corrected value -----------------
    idx = take("date_format_dmy", REPAIR_RATES["date_format_dmy"])
    df.loc[idx, "sale_date"] = [pd.Timestamp(clean.at[i, "sale_date"]).strftime("%d/%m/%Y") for i in idx]
    idx = take("money_symbols", REPAIR_RATES["money_symbols"])
    df.loc[idx, "revenue"] = [f"${float(df.at[i, 'revenue']):,.2f}" for i in idx]
    idx = take("units_as_float", REPAIR_RATES["units_as_float"])
    df.loc[idx, "units_sold"] = [f"{df.at[i, 'units_sold']}.0" for i in idx]
    idx = take("item_name_case_whitespace", REPAIR_RATES["item_name_case_whitespace"])
    df.loc[idx, "item_name"] = [_messy_text(df.at[i, "item_name"], rng) for i in idx]
    idx = take("category_case_whitespace", REPAIR_RATES["category_case_whitespace"])
    df.loc[idx, "category"] = [_messy_text(df.at[i, "category"], rng) for i in idx]
    idx = take("missing_category", REPAIR_RATES["missing_category"])
    df.loc[idx, "category"] = ""

    # ---- duplicates: copies are placed AFTER their original so "keep first" is well-defined ----------
    clean_pool = np.array(perm[cursor:])           # rows that received no corruption at all
    n_exact = int(round(n * DUPLICATE_RATES["duplicate_row"]))
    n_conf = int(round(n * DUPLICATE_RATES["conflicting_duplicate"]))
    picks = rng.choice(clean_pool, size=n_exact + n_conf, replace=False)
    df["_key"] = np.arange(n, dtype=float)
    copies = df.loc[picks].copy()
    copies["_key"] = copies["_key"] + rng.uniform(0.2, 400, len(copies))
    exact, conflicting = copies.iloc[:n_exact].copy(), copies.iloc[n_exact:].copy()
    exact["item_name"] = [_messy_text(v, rng) if rng.random() < 0.5 else v for v in exact["item_name"]]
    conflicting["units_sold"] = [str(int(u) + int(rng.integers(3, 9))) for u in conflicting["units_sold"]]
    injected["duplicate_row"], injected["conflicting_duplicate"] = n_exact, n_conf
    out = pd.concat([df, exact, conflicting]).sort_values("_key", kind="stable").drop(columns="_key")
    out = out.reset_index(drop=True)

    reject_total = sum(injected[k] for k in REJECT_RATES) + n_exact + n_conf
    manifest = {
        "base_rows": n,
        "total_rows_written": int(len(out)),
        "injected_rejections": {k: injected[k] for k in REJECT_RATES}
        | {"duplicate_row": n_exact, "conflicting_duplicate": n_conf},
        "injected_repairs": {k: injected[k] for k in REPAIR_RATES},
        "expected_rows_rejected": int(reject_total),
        "expected_rows_loaded": int(len(out) - reject_total),
    }
    return out, manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "data" / "raw" / "sales_raw.csv")
    ap.add_argument("--clean", action="store_true", help="write the clean data without injected problems")
    args = ap.parse_args()

    date.fromisoformat(args.start), date.fromisoformat(args.end)   # fail fast on bad dates
    clean = generate_clean(args.start, args.end, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.clean:
        clean.assign(sale_date=clean.sale_date.dt.strftime("%Y-%m-%d")).to_csv(args.out, index=False)
        print(f"wrote {len(clean):,} clean rows -> {args.out}")
        return
    dirty, manifest = inject_dirt(clean, args.seed)
    dirty.to_csv(args.out, index=False)
    manifest_path = args.out.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"wrote {len(dirty):,} rows -> {args.out}")
    print(f"ground-truth manifest -> {manifest_path}")
    print(json.dumps({k: manifest[k] for k in ("base_rows", "expected_rows_rejected", "expected_rows_loaded")}))


if __name__ == "__main__":
    main()
