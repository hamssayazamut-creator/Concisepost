"""
server.main
===========

concisepost SaaS backend (FastAPI + asyncpg + PostgreSQL/Supabase).

Endpoints
---------
- ``GET  /health``                      Liveness + DB ping (no auth).
- ``POST /api/v1/telemetry``            Ingest one optimized-message event into
                                        ``usage_logs``. Returns **202 Accepted**
                                        immediately and writes in the background.
- ``GET  /api/v1/dashboard/summary``    Tenant analytics + current-month quota.

Security
--------
Every ``/api/v1/*`` endpoint requires the ``X-ConcisePost-API-Key`` header. The
raw key is SHA-256 hashed and matched against ``api_keys.key_hash`` — the
plaintext key is never stored or logged.

Quota
-----
Plan limits (optimized messages / month) live in the ``plans`` table:
    Free 1,000 · Pro 25,000 · Team 100,000 · Enterprise 1,000,000 ($499/mo)
A NULL limit (if ever configured) is treated as uncapped and never throttled.

Run
---
    export DATABASE_URL='postgresql://user:pass@host:5432/concisepost'
    # The schema is applied automatically on startup (AUTO_MIGRATE=true default),
    # so no manual psql step is required. To disable, set AUTO_MIGRATE=0.
    uvicorn server.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import asyncpg
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Request,
    Security,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class Settings:
    """Runtime configuration sourced from environment variables."""

    def __init__(self) -> None:
        self.database_url: str = os.environ.get("DATABASE_URL", "")
        self.pool_min_size: int = int(os.environ.get("DB_POOL_MIN", "2"))
        self.pool_max_size: int = int(os.environ.get("DB_POOL_MAX", "10"))
        self.command_timeout: float = float(os.environ.get("DB_CMD_TIMEOUT", "10"))
        self.cors_origins: list[str] = [
            o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()
        ]


settings = Settings()

logger = logging.getLogger("concisepost.server")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | concisepost.server | %(message)s"
    ))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Pydantic v2 schemas
# --------------------------------------------------------------------------- #
class TelemetryIn(BaseModel):
    """Inbound telemetry payload for a single optimized agent message."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=256,
                          description="Logical identifier of the emitting agent.")
    original_tokens: int = Field(ge=0, le=10_000_000,
                                 description="Token count before optimization.")
    optimized_tokens: int = Field(ge=0, le=10_000_000,
                                  description="Token count after optimization.")
    cost_saved_usd: Decimal = Field(ge=0, max_digits=14, decimal_places=6,
                                    description="Locally-estimated USD saved.")
    loop_prevented: bool = Field(default=False,
                                 description="True if an agent loop was cut short.")


class TelemetryAck(BaseModel):
    """202 acknowledgement returned before the background write completes."""
    status: str
    accepted_at: str


class DashboardSummary(BaseModel):
    """Aggregated analytics + current-month quota for the authenticated tenant."""
    company_id: str
    tier: str
    total_optimized_messages: int
    total_raw_tokens_saved: int
    cumulative_usd_saved: float
    loops_prevented_count: int
    percentage_efficiency: float
    monthly_message_limit: Optional[int]   # null = uncapped tier (no ceiling)
    optimized_messages_this_month: int
    quota_remaining: Optional[int]         # null = uncapped tier (no ceiling)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
API_KEY_HEADER = APIKeyHeader(name="X-ConcisePost-API-Key", auto_error=False)


@dataclass
class AuthContext:
    """Resolved tenant identity for an authenticated request."""
    company_id: str
    tier: str
    monthly_message_limit: Optional[int]   # None = uncapped tier (no ceiling)


def _hash_key(raw_key: str) -> str:
    """SHA-256 hex of the raw API key. Matches api_keys.key_hash in the DB."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class ApiError(Exception):
    """Application error mapped to a JSON HTTP response."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


async def get_pool(request: Request) -> asyncpg.Pool:
    """Return the shared asyncpg pool from app state."""
    pool: Optional[asyncpg.Pool] = getattr(request.app.state, "pool", None)
    if pool is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE,
                       "db_unavailable", "Database pool is not initialized.")
    return pool


async def authenticate(
    request: Request,
    api_key: Optional[str] = Security(API_KEY_HEADER),
) -> AuthContext:
    """
    Validate the ``X-ConcisePost-API-Key`` header and resolve the tenant.

    Raises :class:`ApiError` 401 when the header is missing or the key is
    unknown/inactive. On success, updates ``last_used_at`` opportunistically.
    """
    if not api_key:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "missing_api_key",
                       "Missing X-ConcisePost-API-Key header.")

    key_hash = _hash_key(api_key)
    pool = await get_pool(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT c.id AS company_id,
                   c.tier::text AS tier,
                   p.monthly_message_limit AS monthly_message_limit
            FROM api_keys k
            JOIN companies c ON c.id = k.company_id
            JOIN plans p ON p.tier = c.tier
            WHERE k.key_hash = $1 AND k.active = true
            """,
            key_hash,
        )
        if row is None:
            raise ApiError(status.HTTP_401_UNAUTHORIZED, "invalid_api_key",
                           "API key is invalid or inactive.")
        # Best-effort last-used stamp; never block auth on it.
        try:
            await conn.execute(
                "UPDATE api_keys SET last_used_at = now() WHERE key_hash = $1",
                key_hash,
            )
        except Exception as exc:  # pragma: no cover - non-critical
            logger.warning("Could not update last_used_at: %s", exc)

    limit_raw = row["monthly_message_limit"]
    return AuthContext(
        company_id=str(row["company_id"]),
        tier=row["tier"],
        monthly_message_limit=int(limit_raw) if limit_raw is not None else None,
    )


# --------------------------------------------------------------------------- #
# DB helpers (all reads/writes on usage_logs run under the tenant RLS GUC)
# --------------------------------------------------------------------------- #
async def _set_tenant(conn: asyncpg.Connection, company_id: str) -> None:
    """Bind the RLS tenant GUC for the current transaction (transaction-local)."""
    await conn.execute(
        "SELECT set_config('app.current_company', $1, true)", company_id
    )


async def _current_month_usage(pool: asyncpg.Pool, company_id: str) -> int:
    """
    Count optimized messages logged for the current UTC month. Backed by the
    composite (company_id, created_at) index, so this is an index range scan.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _set_tenant(conn, company_id)
            val = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM usage_logs
                WHERE company_id = $1
                  AND created_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
                """,
                company_id,
            )
    return int(val) if val is not None else 0


async def _persist_event(pool: asyncpg.Pool, company_id: str,
                         payload: TelemetryIn) -> None:
    """
    Background writer: append one row to ``usage_logs``. Failures are logged,
    never raised (the client already received its 202 Accepted).
    """
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _set_tenant(conn, company_id)
                await conn.execute(
                    """
                    INSERT INTO usage_logs (
                        company_id, agent_id, original_tokens,
                        optimized_tokens, cost_saved_usd, loop_prevented
                    ) VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    company_id,
                    payload.agent_id,
                    payload.original_tokens,
                    payload.optimized_tokens,
                    payload.cost_saved_usd,
                    payload.loop_prevented,
                )
    except Exception as exc:
        logger.error("Background usage_logs insert failed for company %s: %s",
                     company_id, exc)


async def _aggregate_summary(pool: asyncpg.Pool, company_id: str) -> dict:
    """
    Full-history analytics for one tenant, computed in a single aggregate scan.
    Returns raw sums; efficiency is derived by the caller.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _set_tenant(conn, company_id)
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)                                         AS total_messages,
                    COALESCE(SUM(original_tokens - optimized_tokens), 0) AS raw_saved,
                    COALESCE(SUM(original_tokens), 0)                AS original_total,
                    COALESCE(SUM(cost_saved_usd), 0)                 AS usd_saved,
                    COUNT(*) FILTER (WHERE loop_prevented)           AS loops_prevented,
                    COUNT(*) FILTER (
                        WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
                    )                                                AS this_month
                FROM usage_logs
                WHERE company_id = $1
                """,
                company_id,
            )
    return dict(row) if row is not None else {}


# --------------------------------------------------------------------------- #
# Lifespan: create/destroy the connection pool
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Lifespan: create/destroy the connection pool (+ optional auto-migration)
# --------------------------------------------------------------------------- #
def _auto_migrate_enabled() -> bool:
    """AUTO_MIGRATE is on by default; set it to 0/false/no/off to disable."""
    return os.environ.get("AUTO_MIGRATE", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


async def _apply_schema(pool: asyncpg.Pool) -> None:
    """
    Apply the bundled ``schema.sql`` once on startup. The script is fully
    idempotent (CREATE ... IF NOT EXISTS / CREATE OR REPLACE / guarded seeds),
    so running it on every boot is safe and means a fresh database needs no
    terminal, no psql, and no manual SQL — ideal for one-click cloud deploys.
    Disable with AUTO_MIGRATE=0 for multi-instance setups that migrate once.
    """
    if not _auto_migrate_enabled():
        logger.info("AUTO_MIGRATE disabled; skipping schema application.")
        return
    schema_path = pathlib.Path(__file__).resolve().parent / "schema.sql"
    if not schema_path.exists():
        logger.warning("AUTO_MIGRATE on but schema.sql not found at %s; skipping.",
                       schema_path)
        return
    sql = schema_path.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        # asyncpg's simple-query protocol runs the whole multi-statement script.
        await conn.execute(sql)
    logger.info("Database schema applied (AUTO_MIGRATE) from %s.", schema_path.name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL environment variable is required.")
    app.state.pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        command_timeout=settings.command_timeout,
    )
    logger.info("asyncpg pool ready (min=%d, max=%d).",
                settings.pool_min_size, settings.pool_max_size)
    try:
        await _apply_schema(app.state.pool)
    except Exception as exc:
        # Never crash the service on a migration hiccup; surface it loudly and
        # let /health report degraded so the operator can investigate.
        logger.error("Schema auto-migration failed: %s", exc)
    try:
        yield
    finally:
        await app.state.pool.close()
        logger.info("asyncpg pool closed.")


app = FastAPI(
    title="concisepost API",
    version="1.0.0",
    description="Inter-agent message optimization telemetry + analytics.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*", "X-ConcisePost-API-Key"],
)


@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
async def health(request: Request) -> dict:
    """Liveness probe with a lightweight DB round-trip."""
    pool: Optional[asyncpg.Pool] = getattr(request.app.state, "pool", None)
    db_ok = False
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                db_ok = (await conn.fetchval("SELECT 1")) == 1
        except Exception as exc:  # pragma: no cover - infra dependent
            logger.warning("Health DB check failed: %s", exc)
    return {
        "status": "ok" if db_ok else "degraded",
        "database": "up" if db_ok else "down",
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.post(
    "/api/v1/telemetry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TelemetryAck,
    tags=["ingestion"],
)
async def ingest_telemetry(
    payload: TelemetryIn,
    background_tasks: BackgroundTasks,
    request: Request,
    auth: AuthContext = Depends(authenticate),
) -> TelemetryAck:
    """
    Ingest one optimized-message telemetry event.

    Flow: authenticate -> fast quota check -> schedule background write ->
    return **202 Accepted** immediately so the client is never blocked on I/O.
    Over-quota tenants receive **429 Too Many Requests**. A tier whose limit
    is NULL is treated as uncapped and never throttled.
    """
    pool = await get_pool(request)

    # Enforce quota only for capped tiers. A NULL limit means "uncapped".
    if auth.monthly_message_limit is not None:
        used = await _current_month_usage(pool, auth.company_id)
        if used >= auth.monthly_message_limit:
            raise ApiError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "quota_exceeded",
                f"Monthly quota of {auth.monthly_message_limit} optimized messages "
                f"reached for tier '{auth.tier}'. Upgrade to raise the limit.",
            )

    # Non-blocking persistence: the actual write happens after the response.
    background_tasks.add_task(_persist_event, pool, auth.company_id, payload)

    return TelemetryAck(
        status="accepted",
        accepted_at=datetime.now(timezone.utc).isoformat(),
    )


@app.get(
    "/api/v1/dashboard/summary",
    response_model=DashboardSummary,
    tags=["analytics"],
)
async def dashboard_summary(
    request: Request,
    auth: AuthContext = Depends(authenticate),
) -> DashboardSummary:
    """
    Aggregated tenant analytics: total optimized messages, raw tokens saved,
    cumulative USD saved, loops prevented, and percentage efficiency — plus the
    current-month quota position. Backed by the composite (company_id,
    created_at) index for sub-millisecond reads.
    """
    pool = await get_pool(request)
    agg = await _aggregate_summary(pool, auth.company_id)

    total_messages = int(agg.get("total_messages", 0) or 0)
    raw_saved = int(agg.get("raw_saved", 0) or 0)
    original_total = int(agg.get("original_total", 0) or 0)
    usd_saved = float(agg.get("usd_saved", 0) or 0.0)
    loops = int(agg.get("loops_prevented", 0) or 0)
    used_month = int(agg.get("this_month", 0) or 0)

    efficiency = round(100.0 * raw_saved / original_total, 2) if original_total > 0 else 0.0

    limit = auth.monthly_message_limit  # None = uncapped tier (no ceiling)
    quota_remaining = max(0, limit - used_month) if limit is not None else None

    return DashboardSummary(
        company_id=auth.company_id,
        tier=auth.tier,
        total_optimized_messages=total_messages,
        total_raw_tokens_saved=raw_saved,
        cumulative_usd_saved=round(usd_saved, 6),
        loops_prevented_count=loops,
        percentage_efficiency=efficiency,
        monthly_message_limit=limit,
        optimized_messages_this_month=used_month,
        quota_remaining=quota_remaining,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("RELOAD")),
    )
