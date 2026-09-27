#!/usr/bin/env python3
"""Score NL-to-SQL correctness on evaluation/nl_sql_questions.jsonl.

Methodology (see docs/ARCHITECTURE.md section 7): for each labeled question, (1) generate SQL with
the real LLM, (2) run it through the actual validator, (3) if valid, execute it and the
hand-written reference SQL, and compare the *results*, not the SQL text -- two different queries can
be equally correct. This is a real, honest evaluation: it is only as good as the reference SQL
(written once, by a human, and worth spot-checking), and only covers the ~20 questions here.

Requires a real LLM_API_KEY in the environment (see .env.example). Without one, this exits with a
clear message rather than fabricating a result -- there is deliberately no "demo mode" that prints a
fake score. To sanity-check the harness itself with no LLM at all, run test_evaluate_nl_sql.py
instead, which replaces the LLM with the reference SQL and asserts the comparison logic scores 100%.

Usage:
    python -m scripts.evaluate_nl_sql                     # uses DATABASE_URL / READONLY_DATABASE_URL
    python -m scripts.evaluate_nl_sql --out results.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.config import PROJECT_ROOT, get_settings
from app.nl_sql.executor import ExecutionError, execute_readonly
from app.nl_sql.llm_client import LLMClient, LLMError
from app.nl_sql.prompt import build_messages
from app.nl_sql.validator import validate_and_limit

QUESTIONS_PATH = PROJECT_ROOT / "evaluation" / "nl_sql_questions.jsonl"


def load_questions(path: Path = QUESTIONS_PATH) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _normalize(rows: list[dict]) -> list[tuple]:
    """Order-insensitive, type-tolerant comparison: sort each row's values by column name, cast
    everything to str. Good enough for this evaluation's questions; a query whose correct answer
    depends on row order (none of them do -- see the ORDER BY in every reference query that needs
    one) would need a stricter comparison."""
    normalized = []
    for row in rows:
        normalized.append(tuple(str(row[k]) for k in sorted(row.keys())))
    return sorted(normalized)


def run_reference(conn: psycopg.Connection, sql: str) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        return cur.fetchall()


def evaluate(sql_generator, rw_dsn: str, ro_dsn: str, max_rows: int, questions: list[dict]) -> dict:
    results = []
    with psycopg.connect(rw_dsn) as rw_conn, psycopg.connect(ro_dsn, row_factory=dict_row) as ro_conn:
        for q in questions:
            entry = {"id": q["id"], "question": q["question"]}
            try:
                llm_result = sql_generator.generate_sql(build_messages(q["question"]))
            except LLMError as exc:
                entry.update(outcome="llm_error", detail=str(exc))
                results.append(entry)
                continue
            entry["generated_sql"] = llm_result.sql

            validation = validate_and_limit(llm_result.sql, max_rows)
            if not validation.ok:
                entry.update(outcome="rejected", detail=validation.reject_detail,
                              reject_reason=validation.reject_reason)
                results.append(entry)
                continue

            try:
                execution = execute_readonly(ro_conn, validation.sql, validation.limit_applied)
            except ExecutionError as exc:
                entry.update(outcome="execution_error", detail=str(exc))
                results.append(entry)
                continue

            generated_rows = [dict(zip(execution.columns, row, strict=True)) for row in execution.rows]
            try:
                reference_rows = run_reference(rw_conn, q["reference_sql"])
            except psycopg.Error as exc:
                rw_conn.rollback()
                entry.update(outcome="reference_sql_error", detail=str(exc))
                results.append(entry)
                continue

            match = _normalize(generated_rows) == _normalize(reference_rows)
            entry.update(outcome="pass" if match else "mismatch",
                          generated_row_count=len(generated_rows), reference_row_count=len(reference_rows))
            results.append(entry)

    n = len(results)
    passed = sum(1 for r in results if r["outcome"] == "pass")
    summary = {
        "total": n,
        "passed": passed,
        "correctness_rate_pct": round(100 * passed / n, 1) if n else None,
        "by_outcome": {k: sum(1 for r in results if r["outcome"] == k)
                       for k in {"pass", "mismatch", "rejected", "llm_error", "execution_error", "reference_sql_error"}},
    }
    return {"summary": summary, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(PROJECT_ROOT / "evaluation" / "results.json"))
    parser.add_argument("--questions", default=str(QUESTIONS_PATH))
    args = parser.parse_args()

    s = get_settings()
    if s.llm_api_key is None:
        print("LLM_API_KEY is not set. This evaluation makes real LLM calls and does not have a "
              "fake/demo mode that prints a fabricated score -- set LLM_API_KEY in your .env and "
              "re-run. See .env.example.", file=sys.stderr)
        return 1

    generator = LLMClient(s.llm_base_url, s.llm_api_key.get_secret_value(), s.llm_model,
                           s.llm_timeout_seconds, s.llm_max_output_tokens)
    questions = load_questions(Path(args.questions))
    report = evaluate(generator, s.database_url, s.readonly_database_url, s.nl_max_rows, questions)

    Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report["summary"], indent=2))
    print(f"\nFull per-question results written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
