"""Feature engineering for the demand model -- ONE function used for both training and prediction.

Using a single `feature_row` for training and for (recursive) prediction means there can be no
train/serve skew: the model sees features computed identically in both places.

The target is units sold on day i. Every feature is knowable BEFORE day i (nothing from day i or later):

  lag_7                  units sold exactly one week earlier (same weekday)
  lag_14                 units sold two weeks earlier
  roll_mean_7_at_lag_7   average of the 7 days ending one week ago (recent level, smoothed)
  trend_years            years since the item's first sale (linear trend)
  dow_tue .. dow_sun     weekday indicator variables (Monday is the baseline)
  doy_sin, doy_cos       one sine/cosine pair with a 1-year period (annual seasonality)

Why lags of 7+ days and not lag_1? A 28-day forecast must feed its own predictions back in as lags
(recursive forecasting). Lag-1 would make each day depend on the previous *prediction*, so errors compound
quickly; lags >= 7 keep the first week of every forecast anchored to real data.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

import numpy as np

MIN_HISTORY = 14        # the largest lag; the first 14 days of a series cannot be used as targets
_DOW = ["dow_tue", "dow_wed", "dow_thu", "dow_fri", "dow_sat", "dow_sun"]
FEATURE_NAMES = ["lag_7", "lag_14", "roll_mean_7_at_lag_7", "trend_years", *_DOW, "doy_sin", "doy_cos"]


def feature_row(values: Sequence[float], target_date: date, i: int, t0: date) -> list[float]:
    """Features for predicting day index `i` (whose date is `target_date`) using only values[:i]."""
    if i < MIN_HISTORY:
        raise ValueError(f"need at least {MIN_HISTORY} days of history, got {i}")
    lag7, lag14 = values[i - 7], values[i - 14]
    roll = sum(values[i - 13: i - 6]) / 7.0          # indices i-13 .. i-7  (7 values, all before i)
    dow = target_date.weekday()                      # Monday = 0
    angle = 2 * math.pi * target_date.timetuple().tm_yday / 365.25
    return [
        float(lag7), float(lag14), roll, (target_date - t0).days / 365.25,
        *[1.0 if dow == k else 0.0 for k in range(1, 7)],
        math.sin(angle), math.cos(angle),
    ]


def build_training_matrix(dates: Sequence[date], values: Sequence[float], observed: Sequence[bool],
                          t0: date) -> tuple[np.ndarray, np.ndarray]:
    """One (features, target) row per day, skipping (a) the first MIN_HISTORY days and (b) days whose
    value was IMPUTED rather than observed -- we never train on numbers we made up."""
    X, y = [], []
    for i in range(MIN_HISTORY, len(values)):
        if observed[i]:
            X.append(feature_row(values, dates[i], i, t0))
            y.append(values[i])
    return np.asarray(X, dtype=float).reshape(-1, len(FEATURE_NAMES)), np.asarray(y, dtype=float)
