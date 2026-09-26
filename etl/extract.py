"""EXTRACT: read the raw CSV with NO type inference.

Everything is read as text on purpose. If we let pandas guess types, a value like "twelve" would turn
a whole numeric column into strings, and "N/A" would silently become NaN. We want to make every
parsing decision ourselves, explicitly, in etl/clean.py.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger("etl.extract")

REQUIRED_COLUMNS = ["sale_date", "item_name", "category", "units_sold", "revenue"]


class ExtractError(Exception):
    """The file itself is unusable (missing, empty, wrong columns). Nothing is loaded."""


def extract_csv(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.is_file():
        raise ExtractError(f"input file not found: {path}")
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ExtractError(f"missing required column(s) {missing}; found {list(df.columns)}")
    if df.empty:
        raise ExtractError("input file has a header but no data rows")
    df = df[REQUIRED_COLUMNS].copy()
    # Line number in the source file (line 1 is the header) -> every rejected row can be traced back.
    df.insert(0, "source_row", range(2, len(df) + 2))
    log.info("extracted %d rows from %s", len(df), path.name)
    return df
