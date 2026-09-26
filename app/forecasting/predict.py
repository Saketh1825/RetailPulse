"""Generate forecasts (recursive multi-step) and store them.

Recursive forecasting: predict tomorrow, append the prediction to the history, use it to build the next
day's features, repeat. Predictions are clipped at 0 -- you cannot sell negative units.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

import psycopg

from app.forecasting.features import feature_row


def recursive_forecast(model, dates: Sequence[date], values: Sequence[float], horizon: int,
                       t0: date) -> list[tuple[date, float]]:
    """Forecast `horizon` days after the last date in `dates`, using ONLY the supplied history."""
    hist = list(values)
    last = dates[-1]
    out: list[tuple[date, float]] = []
    for step in range(1, horizon + 1):
        d = last + timedelta(days=step)
        x = feature_row(hist, d, len(hist), t0)
        yhat = max(0.0, float(model.predict([x])[0]))
        hist.append(yhat)
        out.append((d, yhat))
    return out


def write_forecasts(conn: psycopg.Connection, item_id: int, forecast: list[tuple[date, float]]) -> None:
    """Replace this item's stored forecast atomically (delete + insert in the caller's transaction)."""
    conn.execute("DELETE FROM forecasts WHERE item_id = %s", (item_id,))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO forecasts (item_id, forecast_date, predicted_units) VALUES (%s, %s, %s)",
            [(item_id, d, y) for d, y in forecast])
