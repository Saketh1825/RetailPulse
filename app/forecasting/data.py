"""Load one item's daily sales as a gap-aware series."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg


@dataclass
class Series:
    dates: list[date]
    values: list[float]        # gaps filled by interpolation (used ONLY to build lag features)
    observed: list[bool]       # True where a real sales row exists; False = imputed gap

    def __len__(self) -> int:
        return len(self.dates)


def build_series(rows: list[tuple[date, int]], end: date) -> Series:
    """rows = [(sale_date, units)] sorted by date. The series runs from the item's first sale to `end`.

    A day with no row is a GAP, not a zero: the ETL rejects invalid rows, so an absent day usually means
    "we lost that record". Real zero-sales days exist in the data as explicit rows with units_sold = 0.
    """
    first = rows[0][0]
    idx = pd.date_range(first, end, freq="D")
    s = pd.Series(np.nan, index=idx)
    for d, u in rows:
        s.loc[pd.Timestamp(d)] = float(u)
    observed = s.notna().tolist()
    filled = s.interpolate(method="linear", limit_direction="both")
    return Series([d.date() for d in idx], filled.tolist(), observed)


def load_item_series(conn: psycopg.Connection, item_id: int, dataset_end: date) -> Series | None:
    rows = conn.execute("SELECT sale_date, units_sold FROM sales WHERE item_id = %s ORDER BY sale_date",
                        (item_id,)).fetchall()
    rows = [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in rows]
    return build_series(rows, dataset_end) if rows else None


def is_active(series: Series, dataset_end: date, horizon: int) -> bool:
    """An item with no real sale in the last `horizon` days is treated as discontinued: forecasting
    demand for a product that has stopped selling would be meaningless."""
    last_real = max(d for d, o in zip(series.dates, series.observed, strict=True) if o)
    return last_real >= dataset_end - timedelta(days=horizon)
