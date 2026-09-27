"""Layer 3+4 of the NL-to-SQL defense: EXECUTE already-validated SQL on the read-only role.

This module must never be given anything the validator (app/nl_sql/validator.py) has not already
approved. It does not re-validate -- that would be redundant and could hide a validator bug behind
a false sense of double-checking. Its only jobs are: run the query on the SELECT-only connection,
time it, and detect truncation.

The read-only role itself (see db/readonly_grants.sql) already has `statement_timeout` and
`default_transaction_read_only = on` set at the role level, so even a bug here cannot turn into a
write or a runaway query -- Postgres enforces that regardless of what this code does.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import psycopg
from psycopg import Connection


class ExecutionError(Exception):
    """Raised for any failure running the query: timeout, permission denial, syntax error the
    validator missed, etc. The caller is expected to show a generic message to the client and log
    the real detail server-side -- never echo a raw database error back to the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ExecutionResult:
    columns: list[str]
    rows: list[list]
    row_count: int
    truncated: bool
    elapsed_ms: int


def execute_readonly(conn: Connection, sql: str, limit_applied: int) -> ExecutionResult:
    """Run `sql` (already validated, already carrying a LIMIT of limit_applied + 1 -- see
    validator._apply_limit) on `conn`, which MUST be a connection using the read-only role.

    Requesting one extra row beyond the cap lets us report `truncated` accurately without a
    second COUNT(*) query.
    """
    t0 = time.perf_counter()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            columns = [d.name for d in cur.description] if cur.description else []
            fetched = cur.fetchall()
    except psycopg.errors.QueryCanceled as exc:
        raise ExecutionError("query_timeout", "the query took too long and was cancelled") from exc
    except (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction) as exc:
        # Either the role's grants (InsufficientPrivilege) or its session-level read-only mode
        # (ReadOnlySqlTransaction, see db/readonly_grants.sql) refused a write. Should be
        # unreachable if the validator is correct -- this is the layer-3 backstop catching a
        # layer-2 miss. Surfacing a distinct code makes that visible in the audit log.
        raise ExecutionError("permission_denied", "the database refused this query") from exc
    except psycopg.Error as exc:
        raise ExecutionError("execution_failed", "the query could not be executed") from exc
    finally:
        conn.rollback()  # release the read-only session's implicit transaction either way

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    truncated = len(fetched) > limit_applied
    rows = fetched[:limit_applied]
    return ExecutionResult(
        columns=columns,
        rows=[[row[c] for c in columns] for row in rows],
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=elapsed_ms,
    )
