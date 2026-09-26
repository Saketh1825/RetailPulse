"""LOAD: write items and sales into PostgreSQL, atomically and idempotently.

* One transaction: either the whole file is loaded or nothing is.
* Bulk path: COPY into a temporary staging table, then one INSERT ... ON CONFLICT DO UPDATE into
  `sales`. Re-running the same file changes nothing (rows are matched on (item_id, sale_date)).
* We report inserted / updated / unchanged rows separately, so a re-run is visibly a no-op.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd
import psycopg

log = logging.getLogger("etl.load")


@dataclass
class LoadStats:
    items_upserted: int
    sales_inserted: int
    sales_updated: int
    sales_unchanged: int

    @property
    def sales_loaded(self) -> int:
        return self.sales_inserted + self.sales_updated + self.sales_unchanged


def load(conn: psycopg.Connection, items: pd.DataFrame, sales: pd.DataFrame) -> LoadStats:
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO items (name, category) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET category = EXCLUDED.category",
            list(items.itertuples(index=False, name=None)),
        )
        cur.execute("SELECT name, id FROM items WHERE name = ANY(%s)", (items["name"].tolist(),))
        id_of = dict(cur.fetchall())

        cur.execute(
            "CREATE TEMP TABLE stg_sales (item_id int, sale_date date, units_sold int, "
            "revenue numeric(10,2)) ON COMMIT DROP"
        )
        with cur.copy("COPY stg_sales (item_id, sale_date, units_sold, revenue) FROM STDIN") as cp:
            for name, d, u, r in sales.itertuples(index=False, name=None):
                cp.write_row((id_of[name], d, int(u), Decimal(f"{r:.2f}")))

        cur.execute(
            """
            INSERT INTO sales (item_id, sale_date, units_sold, revenue)
            SELECT item_id, sale_date, units_sold, revenue FROM stg_sales
            ON CONFLICT (item_id, sale_date) DO UPDATE
                SET units_sold = EXCLUDED.units_sold, revenue = EXCLUDED.revenue
                WHERE (sales.units_sold, sales.revenue) IS DISTINCT FROM (EXCLUDED.units_sold, EXCLUDED.revenue)
            RETURNING (xmax = 0) AS inserted
            """
        )
        flags = [r[0] for r in cur.fetchall()]     # only rows actually inserted or changed come back
    inserted = sum(flags)
    updated = len(flags) - inserted
    stats = LoadStats(len(items), inserted, updated, len(sales) - len(flags))
    log.info("loaded: %s", stats)
    return stats
