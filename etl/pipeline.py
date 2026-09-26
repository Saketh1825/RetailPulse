"""ETL orchestrator:  Extract -> Validate -> Clean -> Transform -> Load -> Analyze.

    python -m etl.pipeline --input data/raw/sales_raw.csv
    python -m etl.pipeline --input data/raw/sales_raw.csv --dry-run     # validate only, no DB writes

Every run is recorded in the `etl_runs` table (also when it fails), and every rejected row is written to
data/rejected/rejected_<timestamp>.csv with the reason it was rejected.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from app.config import get_settings
from etl.clean import Rules, clean_and_validate
from etl.extract import ExtractError, extract_csv
from etl.load import LoadStats, load
from etl.transform import transform

log = logging.getLogger("etl.pipeline")


@dataclass
class EtlReport:
    source_file: str
    rows_read: int
    rows_rejected: int
    rows_loaded: int
    items_loaded: int
    rejections: dict[str, int]
    repairs: dict[str, int]
    rejected_file: str | None
    load_stats: LoadStats | None = None
    analysis: dict = field(default_factory=dict)
    run_id: int | None = None

    def render(self) -> str:
        lines = [
            "", "=" * 64, f" ETL REPORT  ({self.source_file})", "=" * 64,
            f" rows read ............ {self.rows_read:>8,}",
            f" rows rejected ........ {self.rows_rejected:>8,}   ({self.rows_rejected / self.rows_read:.2%})",
            f" rows clean ........... {self.rows_read - self.rows_rejected:>8,}",
        ]
        if self.load_stats:
            ls = self.load_stats
            lines += [f" sales inserted ....... {ls.sales_inserted:>8,}",
                      f" sales updated ........ {ls.sales_updated:>8,}",
                      f" sales unchanged ...... {ls.sales_unchanged:>8,}",
                      f" items ................ {self.items_loaded:>8,}"]
        lines += ["", " rejected rows by reason:"]
        lines += [f"   {k:<30}{v:>6,}" for k, v in sorted(self.rejections.items(), key=lambda kv: -kv[1])]
        lines += ["", " repairs applied (row kept, value corrected):"]
        lines += [f"   {k:<30}{v:>6,}" for k, v in sorted(self.repairs.items(), key=lambda kv: -kv[1])]
        if self.rejected_file:
            lines += ["", f" rejected rows saved to: {self.rejected_file}"]
        if self.analysis:
            lines += ["", " post-load analysis:"] + [f"   {k}: {v}" for k, v in self.analysis.items()]
        return "\n".join(lines + ["=" * 64])


def _write_rejected(rejected: pd.DataFrame, directory: Path) -> str | None:
    if rejected.empty:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rejected_{datetime.now():%Y%m%d_%H%M%S}.csv"
    rejected.to_csv(path, index=False)
    return str(path)


def _analyze(conn: psycopg.Connection) -> dict:
    row = conn.execute(
        "SELECT count(*), min(sale_date), max(sale_date), sum(revenue) FROM sales"
    ).fetchone()
    top = conn.execute(
        "SELECT i.category, sum(s.revenue) r FROM sales s JOIN items i ON i.id = s.item_id "
        "GROUP BY i.category ORDER BY r DESC LIMIT 1"
    ).fetchone()
    return {"sales_rows_in_db": row[0], "date_range": f"{row[1]} .. {row[2]}",
            "total_revenue": f"{row[3]:,.2f}", "top_category": f"{top[0]} ({top[1]:,.2f})"}


def run_etl(input_path: str | Path, dsn: str | None = None, dry_run: bool = False,
            rules: Rules | None = None, rejected_dir: Path | None = None) -> EtlReport:
    settings = get_settings()
    rules = rules or Rules.from_settings(settings)
    dsn = dsn or settings.database_url
    input_path = Path(input_path)

    raw = extract_csv(input_path)                                   # 1. EXTRACT
    result = clean_and_validate(raw, rules)                         # 2-3. VALIDATE + CLEAN
    transformed = transform(result.clean)                           # 4. TRANSFORM
    repairs = {**result.repairs, **transformed.repairs}
    rejected_file = None if dry_run else _write_rejected(result.rejected, rejected_dir or settings.rejected_dir)
    report = EtlReport(input_path.name, result.rows_read, result.rows_rejected, len(transformed.sales),
                       len(transformed.items), result.rejections, repairs, rejected_file)
    if dry_run:
        log.info("dry run: nothing written to the database")
        return report

    with psycopg.connect(dsn) as conn:                              # 5. LOAD
        run_id = conn.execute(
            "INSERT INTO etl_runs (source_file, status, rows_read) VALUES (%s, 'running', %s) RETURNING id",
            (input_path.name, result.rows_read)).fetchone()[0]
        conn.commit()
        try:
            report.load_stats = load(conn, transformed.items, transformed.sales)
            report.analysis = _analyze(conn)                        # 6. ANALYZE
            conn.execute(
                "UPDATE etl_runs SET finished_at = now(), status = 'success', rows_rejected = %s, "
                "rows_loaded = %s, items_loaded = %s, rejection_summary = %s, repair_summary = %s, "
                "rejected_file = %s WHERE id = %s",
                (result.rows_rejected, len(transformed.sales), len(transformed.items),
                 Jsonb(result.rejections), Jsonb(repairs), rejected_file, run_id))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            conn.execute("UPDATE etl_runs SET finished_at = now(), status = 'failed', error_message = %s "
                         "WHERE id = %s", (str(exc)[:500], run_id))
            conn.commit()
            raise
    report.run_id = run_id
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RetailPulse ETL")
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true", help="validate and report, do not touch the database")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        report = run_etl(args.input, dry_run=args.dry_run)
    except ExtractError as exc:
        log.error("cannot process file: %s", exc)
        return 2
    print(json.dumps(report.__dict__, default=lambda o: o.__dict__, indent=2) if args.json else report.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
