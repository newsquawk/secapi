import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
import uvicorn

from config import (
    DEBUG,
    ENABLE_DOCS,
    limiter,
    logger,
    COMMON_STOCK_TITLE_OF_CLASS,
    OPTION_PUT_CALL_NAMES,
)
from database import (
    init_db_pool,
    close_db_pool,
    init_ai_summary_table,
    validate_production_config,
    get_db_cursor,
    get_db_connection,
    _get_db_connection_params,
    _is_connection_healthy,
    db_pool,
    INTERNAL_ERROR_DETAIL,
)
from routers import (
    filings,
    activity,
    sync,
    flow,
    comparisons,
)
from routers.activity import (
    LATEST_ACTIVITY_QUERY_V3,
    LATEST_ACTIVITY_QUERY_OPTIONS,
    MODIFIED_OPTIMISED_STORIES_QUERY,
    FILING_QUERY_V2,
    FILTER_CANDIDATES_QUERY_V2,
    FILING_QUERY_OPTIONS,
)
from routers.comparisons import (
    compare_holdings,
    _process_filings,
)
from utils import (
    resolve_identifiers,
    get_free_float,
)


# ---------------------------------------------------------------------------
# Application Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_production_config()
    logger.info("secapi starting")
    init_db_pool()
    init_ai_summary_table()
    yield
    logger.info("secapi shutting down")
    close_db_pool()


# ---------------------------------------------------------------------------
# FastAPI Initialization
# ---------------------------------------------------------------------------
app = FastAPI(
    title="SEC API",
    version="1.0.0",
    debug=DEBUG,
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)

# Rate limiter setup
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# CORS setup
origins_env = os.environ.get("CORS_ORIGINS", "").split(",")
allow_origins = [origin.strip() for origin in origins_env if origin.strip()]
logger.info(
    f"CORS allowed origins: {allow_origins if allow_origins else '<none> (cross-origin requests blocked)'}"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Trusted hosts validation
allowed_hosts_env = os.getenv("ALLOWED_HOSTS", "")
allowed_hosts = [h.strip() for h in allowed_hosts_env.split(",") if h.strip()]
if allowed_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    logger.info(f"Trusted hosts: {allowed_hosts}")


# ---------------------------------------------------------------------------
# Middleware: Security Headers & Request Timing
# ---------------------------------------------------------------------------
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-XSS-Protection", "0")
    return response


@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)

    start_time = time.perf_counter()
    raw_forwarded = request.headers.get("x-forwarded-for")
    client_ip = (
        raw_forwarded.split(",")[0].strip()
        if raw_forwarded
        else (request.client.host if request.client else "unknown")
    )
    url_path = (
        f"{request.url.path}?{request.url.query}"
        if request.url.query
        else request.url.path
    )

    try:
        response = await call_next(request)
        process_time = (time.perf_counter() - start_time) * 1000
        response.headers["X-Process-Time"] = f"{process_time:.2f}ms"
        logger.info(
            f"{request.method} {url_path} -> {response.status_code} ({process_time:.2f}ms) [{client_ip}]"
        )
        return response
    except Exception as exc:
        process_time = (time.perf_counter() - start_time) * 1000
        logger.error(
            f"{request.method} {url_path} -> FAILED: {exc} ({process_time:.2f}ms) [{client_ip}]"
        )
        raise


# ---------------------------------------------------------------------------
# Core System Probes
# ---------------------------------------------------------------------------
@app.get("/")
def read_root():
    return {"message": "Welcome to the SEC API"}


@app.get("/health")
def health():
    """Liveness probe for load balancers and container healthchecks."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Mount Routers
# ---------------------------------------------------------------------------
app.include_router(filings.router)
app.include_router(activity.router)
app.include_router(sync.router)
app.include_router(flow.router)
app.include_router(comparisons.router)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
