"""POST /query -- natural language in, a validated read-only SQL result out.

Pipeline, matching docs/NL_SQL_SECURITY.md exactly:
  1. (optional) API key check + rate limit -- request-level protection, not SQL safety
  2. LLM turns the question into SQL (app.nl_sql.prompt + app.nl_sql.llm_client)
  3. The validator independently checks that SQL (app.nl_sql.validator) -- SELECT-only, table
     whitelist, no comments/dangerous functions, LIMIT enforced
  4. The (only if valid) SQL executes on a connection using the SELECT-only database role
     (app.nl_sql.executor), which itself carries a statement_timeout
Every attempt -- accepted, rejected, or errored -- is written to nl_query_log for audit, using the
normal read/write role (the read-only role cannot write to that table, by design).
"""
from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from psycopg import Connection

from app.config import Settings, get_settings
from app.db import get_ro_conn
from app.models import QueryRequest, QueryResponse, QueryTimings
from app.nl_sql.executor import ExecutionError, execute_readonly
from app.nl_sql.llm_client import LLMClient, LLMError
from app.nl_sql.prompt import build_messages
from app.nl_sql.validator import validate_and_limit
from app.security import RateLimitExceeded, check_api_key

log = logging.getLogger("app.nl_query")
router = APIRouter(tags=["nl-query"])

RoConn = Annotated[Connection, Depends(get_ro_conn)]


def get_sql_generator():
    """Builds the real LLMClient from settings. Overridden in tests (app.dependency_overrides) with
    a FakeLLMClient so the endpoint can be tested with no network access and no API key."""
    s = get_settings()
    if s.llm_api_key is None:
        raise HTTPException(503, "the natural-language query feature is not configured "
                                  "(no LLM_API_KEY set) -- see .env.example")
    return LLMClient(s.llm_base_url, s.llm_api_key.get_secret_value(), s.llm_model,
                      s.llm_timeout_seconds, s.llm_max_output_tokens)


def _log_attempt(conn: Connection, *, question: str, generated_sql: str | None, executed_sql: str | None,
                  status: str, reject_reason: str | None, row_count: int | None,
                  llm_ms: int | None, db_ms: int | None, total_ms: int) -> None:
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO nl_query_log (question, generated_sql, executed_sql, status, "
                "reject_reason, row_count, llm_ms, db_ms, total_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (question, generated_sql, executed_sql, status, reject_reason, row_count,
                 llm_ms, db_ms, total_ms))
    except Exception:  # noqa: BLE001 - audit logging must never break the actual response
        log.exception("failed to write nl_query_log entry")


@router.post("/query", response_model=QueryResponse, summary="Ask a question about the retail data in plain English")
def run_query(
    body: QueryRequest,
    request: Request,
    ro_conn: RoConn,
    settings: Annotated[Settings, Depends(get_settings)],
    sql_generator=Depends(get_sql_generator),
    x_api_key: Annotated[str | None, Header()] = None,
):
    if not check_api_key(settings.api_key.get_secret_value() if settings.api_key else None, x_api_key):
        raise HTTPException(401, "missing or invalid X-API-Key")

    client_key = request.client.host if request.client else "unknown"
    try:
        request.app.state.query_rate_limiter.check(client_key)
    except RateLimitExceeded as exc:
        raise HTTPException(429, f"rate limit exceeded; retry after {exc.retry_after_seconds}s") from exc

    question = body.question
    if len(question) > settings.nl_question_max_chars:
        raise HTTPException(422, f"question exceeds {settings.nl_question_max_chars} characters")

    t_total0 = time.perf_counter()
    rw_conn = request.app.state.rw_pool.getconn()
    try:
        # 1. LLM: question -> SQL
        try:
            result = sql_generator.generate_sql(build_messages(question))
        except LLMError as exc:
            total_ms = int((time.perf_counter() - t_total0) * 1000)
            _log_attempt(rw_conn, question=question, generated_sql=None, executed_sql=None,
                         status="llm_error", reject_reason=str(exc), row_count=None,
                         llm_ms=None, db_ms=None, total_ms=total_ms)
            raise HTTPException(502, f"could not generate SQL for this question: {exc}") from exc

        # 2. Validate independently of what the prompt asked for
        t_validate0 = time.perf_counter()
        validation = validate_and_limit(result.sql, settings.nl_max_rows)
        validate_ms = int((time.perf_counter() - t_validate0) * 1000)

        if not validation.ok:
            total_ms = int((time.perf_counter() - t_total0) * 1000)
            _log_attempt(rw_conn, question=question, generated_sql=result.sql, executed_sql=None,
                         status="rejected", reject_reason=validation.reject_reason, row_count=None,
                         llm_ms=result.elapsed_ms, db_ms=None, total_ms=total_ms)
            raise HTTPException(422, f"query rejected: {validation.reject_detail}")

        # 3. Execute on the read-only role
        try:
            execution = execute_readonly(ro_conn, validation.sql, validation.limit_applied)
        except ExecutionError as exc:
            total_ms = int((time.perf_counter() - t_total0) * 1000)
            _log_attempt(rw_conn, question=question, generated_sql=result.sql, executed_sql=validation.sql,
                         status="execution_error", reject_reason=exc.code, row_count=None,
                         llm_ms=result.elapsed_ms, db_ms=None, total_ms=total_ms)
            raise HTTPException(504 if exc.code == "query_timeout" else 500, str(exc)) from exc

        total_ms = int((time.perf_counter() - t_total0) * 1000)
        _log_attempt(rw_conn, question=question, generated_sql=result.sql, executed_sql=validation.sql,
                     status="ok", reject_reason=None, row_count=execution.row_count,
                     llm_ms=result.elapsed_ms, db_ms=execution.elapsed_ms, total_ms=total_ms)

        return QueryResponse(
            question=question, sql=validation.sql, columns=execution.columns, rows=execution.rows,
            row_count=execution.row_count, truncated=execution.truncated,
            limit_applied=validation.limit_applied,
            timings=QueryTimings(llm_ms=result.elapsed_ms, validate_ms=validate_ms,
                                  db_ms=execution.elapsed_ms, total_ms=total_ms),
            notes=validation.notes)
    finally:
        request.app.state.rw_pool.putconn(rw_conn)
