"""Train, evaluate and persist the per-item demand models.

    python -m app.forecasting.train

Per active item:
  1. EVALUATE honestly: train on everything before a cutoff, forecast the next `horizon` days recursively
     (seeing NOTHING after the cutoff), compare with what really happened. Repeated at several earlier
     cutoffs ("rolling origin") so the score is not one lucky/unlucky window.
  2. Compare against two trivial baselines (seasonal-naive, recent mean).
  3. REFIT on all data, save the model, write the next `horizon` days of forecasts to PostgreSQL.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import psycopg
from psycopg.types.json import Jsonb
from sklearn.linear_model import LinearRegression

from app.config import get_settings
from app.forecasting.data import Series, is_active, load_item_series
from app.forecasting.features import FEATURE_NAMES, MIN_HISTORY, build_training_matrix
from app.forecasting.metrics import mae, wape
from app.forecasting.predict import recursive_forecast, write_forecasts

log = logging.getLogger("forecasting.train")
N_ORIGINS = 4            # rolling-origin backtest: cutoffs at  T-H, T-2H, T-3H, T-4H


@dataclass
class CutoffResult:
    cutoff: date                    # first day of the test window
    mae: float
    wape: float
    naive_mae: float
    mean_mae: float
    actual: list[float]
    predicted: list[float]
    observed: list[bool]
    n_train_rows: int


@dataclass
class ItemResult:
    item_id: int
    name: str
    status: str                                       # trained | skipped_inactive | skipped_short_history
    origins: list[CutoffResult] = field(default_factory=list)
    coefficients: dict[str, float] = field(default_factory=dict)
    forecast: list[tuple[date, float]] = field(default_factory=list)
    train_start: date | None = None
    train_end: date | None = None
    n_final_rows: int = 0

    @property
    def primary(self) -> CutoffResult:                # the most recent window == the spec's train/test split
        return self.origins[0]


def _fit(series: Series, upto: int, t0: date) -> tuple[LinearRegression, int]:
    X, y = build_training_matrix(series.dates[:upto], series.values[:upto], series.observed[:upto], t0)
    return LinearRegression().fit(X, y), len(y)


def evaluate_at_cutoff(series: Series, cutoff: int, horizon: int) -> CutoffResult:
    """Train on days [0, cutoff), forecast days [cutoff, cutoff+horizon). The holdout is never touched
    until the forecast is finished. (tests/test_forecasting.py proves this by changing the holdout and
    asserting that the predictions do not move.)"""
    t0 = series.dates[0]
    model, n_rows = _fit(series, cutoff, t0)
    pred = [y for _, y in recursive_forecast(model, series.dates[:cutoff], series.values[:cutoff], horizon, t0)]
    actual = series.values[cutoff:cutoff + horizon]
    obs = series.observed[cutoff:cutoff + horizon]
    last_week = series.values[cutoff - 7:cutoff]                      # baseline 1: repeat last week
    naive = [last_week[h % 7] for h in range(horizon)]
    recent_mean = float(np.mean(series.values[cutoff - 28:cutoff]))   # baseline 2: repeat 4-week mean
    return CutoffResult(
        series.dates[cutoff], mae(actual, pred, obs), wape(actual, pred, obs),
        mae(actual, naive, obs), mae(actual, [recent_mean] * horizon, obs),
        list(actual), pred, list(obs), n_rows)


def train_item(item_id: int, name: str, series: Series, dataset_end: date, horizon: int,
               min_history: int) -> ItemResult:
    if not is_active(series, dataset_end, horizon):
        return ItemResult(item_id, name, "skipped_inactive")
    n = len(series)
    if n < min_history + horizon:
        return ItemResult(item_id, name, "skipped_short_history")
    res = ItemResult(item_id, name, "trained", train_start=series.dates[0], train_end=series.dates[n - horizon - 1])
    for k in range(N_ORIGINS):
        cutoff = n - horizon * (k + 1)
        if cutoff >= max(min_history, MIN_HISTORY + 28):
            res.origins.append(evaluate_at_cutoff(series, cutoff, horizon))
    t0 = series.dates[0]
    final, res.n_final_rows = _fit(series, n, t0)                     # refit on ALL data for production
    res.coefficients = {**dict(zip(FEATURE_NAMES, map(float, final.coef_), strict=True)),
                        "intercept": float(final.intercept_)}
    res.forecast = recursive_forecast(final, series.dates, series.values, horizon, t0)
    res._model = final                                                # noqa: SLF001 - saved by caller
    return res


def train_all(dsn: str | None = None, horizon: int | None = None, model_dir: Path | None = None,
              metrics_path: Path | None = None) -> list[ItemResult]:
    s = get_settings()
    horizon, model_dir = horizon or s.forecast_horizon_days, model_dir or s.model_dir
    model_dir.mkdir(parents=True, exist_ok=True)
    results: list[ItemResult] = []
    with psycopg.connect(dsn or s.database_url) as conn:
        end = conn.execute("SELECT max(sale_date) FROM sales").fetchone()[0]
        if end is None:
            raise SystemExit("no sales data: run the ETL first")
        items = conn.execute("SELECT id, name FROM items ORDER BY id").fetchall()
        for item_id, name in items:
            series = load_item_series(conn, item_id, end)
            res = train_item(item_id, name, series, end, horizon, s.forecast_min_history_days) if series \
                else ItemResult(item_id, name, "skipped_short_history")
            results.append(res)
            if res.status != "trained":
                log.info("skipped %-26s (%s)", name, res.status)
                continue
            p = res.primary
            joblib.dump({"model": res._model, "features": FEATURE_NAMES, "t0": series.dates[0],   # noqa: SLF001
                         "trained_through": series.dates[-1], "item_id": item_id}, model_dir / f"item_{item_id}.joblib")
            with conn.transaction():
                write_forecasts(conn, item_id, res.forecast)
                conn.execute(
                    """INSERT INTO forecast_models (item_id, trained_at, model_type, train_start, train_end, test_start,
                          test_end, n_train_rows, test_mae, test_wape, baseline_seasonal_naive_mae, baseline_mean_mae,
                          features, coefficients)
                       VALUES (%s, now(), 'LinearRegression', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (item_id) DO UPDATE SET trained_at = now(), model_type = EXCLUDED.model_type,
                          train_start = EXCLUDED.train_start, train_end = EXCLUDED.train_end,
                          test_start = EXCLUDED.test_start, test_end = EXCLUDED.test_end,
                          n_train_rows = EXCLUDED.n_train_rows, test_mae = EXCLUDED.test_mae,
                          test_wape = EXCLUDED.test_wape,
                          baseline_seasonal_naive_mae = EXCLUDED.baseline_seasonal_naive_mae,
                          baseline_mean_mae = EXCLUDED.baseline_mean_mae, features = EXCLUDED.features,
                          coefficients = EXCLUDED.coefficients""",
                    (item_id, res.train_start, res.train_end, p.cutoff, series.dates[-1], p.n_train_rows, p.mae, p.wape,
                     p.naive_mae, p.mean_mae, Jsonb(FEATURE_NAMES), Jsonb(res.coefficients)))
            log.info("%-26s MAE %.2f (naive %.2f, mean %.2f) WAPE %.1f%%", name, p.mae, p.naive_mae, p.mean_mae, 100 * p.wape)
        # drop stored forecasts of items that are no longer forecastable (e.g. discontinued)
        stale = [r.item_id for r in results if r.status != "trained"]
        if stale:
            conn.execute("DELETE FROM forecasts WHERE item_id = ANY(%s)", (stale,))
            conn.execute("DELETE FROM forecast_models WHERE item_id = ANY(%s)", (stale,))
        conn.commit()
    if metrics_path:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps(summarise(results, horizon), indent=2, default=str))
    return results


def summarise(results: list[ItemResult], horizon: int) -> dict:
    trained = [r for r in results if r.status == "trained"]
    prim = [r.primary for r in trained]
    all_pts = [c for r in trained for c in r.origins]
    def tot(cs, f): return float(sum(np.sum(np.abs(np.array(c.actual) - np.array(f(c)))[np.array(c.observed)]) for c in cs))
    denom = float(sum(np.sum(np.array(c.actual)[np.array(c.observed)]) for c in prim))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": "LinearRegression (one per item)", "horizon_days": horizon, "features": FEATURE_NAMES,
        "items_trained": len(trained), "items_skipped": {r.name: r.status for r in results if r.status != "trained"},
        "primary_window": {
            "test_start": str(prim[0].cutoff) if prim else None,
            "macro_mae_model": float(np.mean([c.mae for c in prim])),
            "macro_mae_seasonal_naive": float(np.mean([c.naive_mae for c in prim])),
            "macro_mae_recent_mean": float(np.mean([c.mean_mae for c in prim])),
            "overall_wape_model": tot(prim, lambda c: c.predicted) / denom,
            "items_beating_seasonal_naive": int(sum(c.mae < c.naive_mae for c in prim)),
            "items_beating_recent_mean": int(sum(c.mae < c.mean_mae for c in prim)),
        },
        "rolling_origin": {
            "windows_per_item": N_ORIGINS,
            "macro_mae_model": float(np.mean([c.mae for c in all_pts])),
            "macro_mae_seasonal_naive": float(np.mean([c.naive_mae for c in all_pts])),
            "macro_mae_recent_mean": float(np.mean([c.mean_mae for c in all_pts])),
            "window_wins_vs_seasonal_naive": f"{sum(c.mae < c.naive_mae for c in all_pts)}/{len(all_pts)}",
        },
        "per_item": [{
            "item_id": r.item_id, "name": r.name,
            "mae": r.primary.mae, "wape": r.primary.wape, "naive_mae": r.primary.naive_mae, "mean_mae": r.primary.mean_mae,
            "origin_maes": [c.mae for c in r.origins], "origin_naive_maes": [c.naive_mae for c in r.origins],
            "test_start": str(r.primary.cutoff), "actual": r.primary.actual, "predicted": r.primary.predicted,
            "observed": r.primary.observed, "coefficients": r.coefficients,
        } for r in trained],
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Train + evaluate + persist per-item Linear Regression forecasts")
    ap.add_argument("--horizon", type=int)
    ap.add_argument("--metrics-out", type=Path, default=Path("artifacts/metrics.json"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    out = train_all(horizon=args.horizon, metrics_path=args.metrics_out)
    m = summarise(out, args.horizon or get_settings().forecast_horizon_days)
    print(json.dumps({k: m[k] for k in ("items_trained", "items_skipped", "primary_window", "rolling_origin")}, indent=2))
