"""TRANSFORM: turn validated rows into the normalized (items, sales) shape.

* Canonical names: "  whole MILK 1l " / "Whole Milk 1L" are the same product. The canonical display
  form is the most common variant seen in the file (ties -> alphabetical, so runs are deterministic).
* Category is an attribute of the ITEM, not of a sale. Each item gets its majority category; a row with
  a blank category inherits it (a counted repair); rows disagreeing with the majority are counted as
  `category_conflict_rows` (reported, not silently hidden).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

UNCATEGORIZED = "Uncategorized"


@dataclass
class Transformed:
    items: pd.DataFrame      # columns: name, category
    sales: pd.DataFrame      # columns: item_name, sale_date, units_sold, revenue
    repairs: dict[str, int]


def _canonical_display(display: pd.Series, key: pd.Series) -> dict[str, str]:
    counts = pd.DataFrame({"key": key, "display": display}).groupby(["key", "display"]).size()
    counts = counts.rename("n").reset_index()
    best = counts.sort_values(["key", "n", "display"], ascending=[True, False, True]).drop_duplicates("key")
    return dict(zip(best["key"], best["display"], strict=True))


def transform(clean: pd.DataFrame) -> Transformed:
    df = clean.copy()
    name_of = _canonical_display(df["item_name"], df["item_key"])
    df["canonical_name"] = df["item_key"].map(name_of)

    cat_missing = df["category"].str.casefold().isin({"", "null", "none", "nan"})
    df["category_key"] = df["category"].str.casefold().where(~cat_missing)
    known = df[~cat_missing]
    cat_display = _canonical_display(known["category"], known["category_key"]) if len(known) else {}

    # item -> majority category key
    if len(known):
        per_item = known.groupby(["item_key", "category_key"]).size().rename("n").reset_index()
        per_item = per_item.sort_values(["item_key", "n", "category_key"], ascending=[True, False, True])
        majority = dict(zip(*per_item.drop_duplicates("item_key")[["item_key", "category_key"]].T.values, strict=True))
    else:
        majority = {}
    item_cat_key = df["item_key"].map(majority)
    df["canonical_category"] = item_cat_key.map(cat_display).fillna(UNCATEGORIZED)

    repairs = {
        "item_name_case_whitespace": int((df["item_name_raw"] != df["canonical_name"]).sum()),
        "category_case_whitespace": int((~cat_missing & (df["category_raw"] != df["canonical_category"])).sum()),
        "missing_category": int(cat_missing.sum()),
        "category_conflict_rows": int((~cat_missing & (df["category_key"] != item_cat_key)).sum()),
    }
    items = (df[["canonical_name", "canonical_category"]].drop_duplicates()
             .rename(columns={"canonical_name": "name", "canonical_category": "category"})
             .sort_values("name").reset_index(drop=True))
    sales = (df[["canonical_name", "sale_date", "units_sold", "revenue"]]
             .rename(columns={"canonical_name": "item_name"})
             .sort_values(["item_name", "sale_date"]).reset_index(drop=True))
    return Transformed(items, sales, repairs)
