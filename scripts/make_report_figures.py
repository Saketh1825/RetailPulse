#!/usr/bin/env python3
"""Generate docs/img/forecast_vs_actual.png from real data: the last 90 days of actual sales for
one item plus its stored forecast, both read from PostgreSQL. Requires `python -m
app.forecasting.train` to have been run first (forecasts must already be in the database).

Usage:
    python -m scripts.make_report_figures [--item-id 1] [--out docs/img/forecast_vs_actual.png]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import psycopg
from psycopg.rows import dict_row

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402 - backend must be set before this import

from app.config import PROJECT_ROOT, get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item-id", type=int, default=None,
                         help="items.id to plot; defaults to whichever item has the lowest holdout MAE")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "docs" / "img" / "forecast_vs_actual.png"))
    args = parser.parse_args()

    settings = get_settings()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        if args.item_id is None:
            row = conn.execute(
                "SELECT item_id FROM forecast_models ORDER BY test_mae ASC LIMIT 1").fetchone()
            if row is None:
                print("No trained models found -- run `python -m app.forecasting.train` first.")
                return 1
            item_id = row["item_id"]
        else:
            item_id = args.item_id

        item = conn.execute("SELECT name, category FROM items WHERE id = %s", (item_id,)).fetchone()
        model = conn.execute(
            "SELECT test_mae, test_wape, train_end, test_end FROM forecast_models WHERE item_id = %s",
            (item_id,)).fetchone()
        actual = conn.execute(
            "SELECT sale_date, units_sold FROM sales WHERE item_id = %s "
            "ORDER BY sale_date DESC LIMIT 90", (item_id,)).fetchall()[::-1]
        forecast = conn.execute(
            "SELECT forecast_date, predicted_units FROM forecasts WHERE item_id = %s "
            "ORDER BY forecast_date", (item_id,)).fetchall()

    if not actual or not forecast or item is None or model is None:
        print(f"Item {item_id} has no data/forecast/model row -- run the ETL and training steps first.")
        return 1

    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot([r["sale_date"] for r in actual], [r["units_sold"] for r in actual],
            label="Actual units sold (last 90 days)", color="#2563eb", linewidth=1.5)
    ax.plot([r["forecast_date"] for r in forecast], [r["predicted_units"] for r in forecast],
            label="Forecast (Linear Regression)", color="#dc2626", linewidth=1.8, linestyle="--")
    ax.axvline([r["sale_date"] for r in actual][-1], color="#9ca3af", linewidth=1, linestyle=":")
    ax.set_title(f"{item['name']} ({item['category']}) -- forecast vs. recent actuals")
    ax.set_ylabel("Units sold / predicted")
    ax.legend(loc="upper left", frameon=False)
    ax.text(0.99, 0.02,
            f"Holdout MAE: {model['test_mae']:.2f} units/day  |  WAPE: {100 * model['test_wape']:.1f}%",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=9, color="#374151")
    fig.autofmt_xdate()
    fig.tight_layout()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    print(f"Wrote {out_path} for item_id={item_id} ({item['name']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
