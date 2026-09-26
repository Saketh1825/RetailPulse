"""Forecast error metrics. Tiny on purpose, so they are easy to verify by hand (see tests)."""
from __future__ import annotations

import numpy as np


def _masked(actual, predicted, mask):
    a, p = np.asarray(actual, dtype=float), np.asarray(predicted, dtype=float)
    m = np.ones(a.shape, dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
    if a.shape != p.shape or a.shape != m.shape:
        raise ValueError("actual, predicted and mask must have the same length")
    return a[m], p[m]


def mae(actual, predicted, mask=None) -> float:
    """Mean Absolute Error, in the target's own unit (units per day). Only days where mask is True count."""
    a, p = _masked(actual, predicted, mask)
    if a.size == 0:
        raise ValueError("no observed days to score")
    return float(np.mean(np.abs(a - p)))


def wape(actual, predicted, mask=None) -> float:
    """Weighted Absolute Percentage Error = sum|error| / sum(actual). Scale-free, so it can compare a
    slow item with a fast item; unlike MAPE it stays defined when some days have zero sales."""
    a, p = _masked(actual, predicted, mask)
    total = float(np.sum(a))
    return float(np.sum(np.abs(a - p)) / total) if total > 0 else float("nan")
