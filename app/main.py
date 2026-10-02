"""FastAPI application factory: wiring, error envelope, request logging."""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.db import make_pool
from app.models import Health
from app.routers import analytics, forecast, nl_query
from app.security import RateLimiter

log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"


def _error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": details}})


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    app.state.rw_pool = make_pool(s.database_url, name="rw", max_size=8, options="-c statement_timeout=15000")
    app.state.ro_pool = make_pool(
        s.readonly_database_url, name="ro", max_size=4,
        options=f"-c statement_timeout={s.nl_statement_timeout_ms} -c default_transaction_read_only=on")
    app.state.query_rate_limiter = RateLimiter(limit=s.query_rate_limit_per_minute)
    yield
    app.state.rw_pool.close()
    app.state.ro_pool.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="RetailPulse",
        version="1.0.0",
        description="Retail data pipeline & demand intelligence: validated ETL -> PostgreSQL -> SQL analytics, "
                    "a Linear Regression demand forecast, and a safety-constrained natural-language SQL interface.",
        lifespan=lifespan,
    )
    origins = [o.strip() for o in get_settings().cors_allow_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["*"])

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        rid, t0 = uuid.uuid4().hex[:8], time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        log.info("%s %s -> %s in %.0fms [%s]", request.method, request.url.path, response.status_code,
                 (time.perf_counter() - t0) * 1000, rid)
        return response

    # ---- one consistent error envelope: {"error": {"code", "message", "details"}} -------------------
    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        return _error(exc.status_code, {404: "not_found", 401: "unauthorized", 429: "rate_limited"}.get(
            exc.status_code, "http_error"), str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        fields = [{"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]} for e in exc.errors()]
        return _error(422, "invalid_request", "request parameters failed validation", {"fields": fields})

    @app.exception_handler(psycopg.Error)
    async def db_error(_: Request, exc: psycopg.Error):
        log.exception("database error")                     # full detail goes to the log ...
        return _error(503, "database_unavailable", "the database could not process the request")   # ... not the client

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        log.exception("unhandled error")
        return _error(500, "internal_error", "unexpected server error")

    @app.get("/health", response_model=Health, tags=["system"], summary="Liveness + dependency check")
    def health(request: Request):
        def ping(pool) -> str:
            try:
                with pool.connection(timeout=3) as c:
                    c.execute("SELECT 1")
                return "ok"
            except Exception:                               # noqa: BLE001 - health must never raise
                return "unavailable"
        db, ro = ping(request.app.state.rw_pool), ping(request.app.state.ro_pool)
        return Health(status="ok" if db == ro == "ok" else "degraded", database=db, readonly_database=ro,
                      llm_configured=get_settings().llm_api_key is not None)

    app.include_router(analytics.router)
    app.include_router(forecast.router)
    app.include_router(nl_query.router)
    if (STATIC / "index.html").exists():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(STATIC / "index.html")
    return app


app = create_app()
