import psycopg2
from fastapi import FastAPI, Depends, HTTPException, Query, Body, Request, Header
import os
import uvicorn
import polars as pl
import asyncio
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from fastapi import HTTPException
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool, PoolError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from typing import List, Dict, Optional
from pydantic import BaseModel
import datetime as dt
import pandas as pd
import logging
import sys
import math
import json
import yfinance as yf

# rate-limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from sec_models import (
    Filing,
    LatestActivityResponse,
    HoldingActivity,
    DailyFlowEntry,
    DailyFlowResponse,
    HoldingsRequest,
    AggregateFlowResponse,
    TopStockChangeEntry,
    TopStockChangesResponse,
)

EDGAR_IDENTITY = os.getenv("EDGAR_IDENTITY", "26b610663e50@company.co.uk")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", None)
COMMON_STOCK_TITLE_OF_CLASS = "COM|CL A|COMMON STOCK|STOCK|COM SHS|CAP STK CL"
# put_or_call names that denote derivative option positions (excluded from comparisons)
OPTION_PUT_CALL_NAMES = frozenset({"PUT", "CALL"})

FREE_FLOAT_DATA = {}
CUSIP_TO_CIK = {}
CIK_TO_TICKER = {}
TICKER_TO_CUSIP = {}
CUSIP_TO_TICKER = {}

RUSSELL_FILE_PATH = "data/russell_share_data.csv"
CUSIP_DETAILS_FILE_PATH = "data/cusip_details_filtered_fixed.csv"
CUSIP_TO_CIK_FILE_PATH = "data/cusip_to_cik.json"
CIK_TO_TICKER_FILE_PATH = "data/cik_to_ticker.json"
TICKER_TO_CUSIP_FILE_PATH = "data/ticker_to_cusip.json"

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
numeric_level = getattr(logging, log_level_str, logging.INFO)

# Set up logging
logging.basicConfig(
    level=numeric_level,  # Change to logging.DEBUG if you want more verbosity
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("secapi")

if DEEPSEEK_API_KEY:
    client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

DEFINED_SCHEMA = {
    "cik": pl.String,
    "company": pl.String,
    "filing_date": pl.Date,
    "accession_no": pl.String,
}

# Load CUSIP details
CUSIP_DETAILS_DF = pl.DataFrame()
try:
    # We only read 'cusip' and 'sic' to minimize memory footprint
    CUSIP_DETAILS_DF = pl.read_csv(
        CUSIP_DETAILS_FILE_PATH,
        columns=["cusip", "industry", "sic", "sicSector", "sicIndustry"],
        schema_overrides={
            "cusip": pl.Utf8,
            "industry": pl.Utf8,
            "sic": pl.Int64,
            "sicSector": pl.Utf8,
            "sicIndustry": pl.Utf8,
        },
        ignore_errors=True,
    ).drop_nulls(subset=["cusip"])

    # Ensure there are no duplicate CUSIPs mapping to multiple SICs
    CUSIP_DETAILS_DF = CUSIP_DETAILS_DF.unique(subset=["cusip"], keep="first")
    logger.info(f"Loaded {CUSIP_DETAILS_DF.height} CUSIP-to-SIC mappings.")
except Exception as e:
    logger.warning(f"Warning: Could not load cusip_details_filtered.csv: {e}")


def load_float_data():
    """Loads the Russell share data into memory on startup."""
    global FREE_FLOAT_DATA
    try:
        # Load the CSV
        df = pd.read_csv(RUSSELL_FILE_PATH)

        # Drop empty rows
        df = df.dropna(subset=["Symbol", "Shr Out less Closely Held Sh"])

        # Build a dictionary where Key = Ticker, Value = Free Float (in millions)
        for _, row in df.iterrows():
            ticker = str(row["Symbol"]).strip().upper()

            # Safely convert the float column to a number, ignoring errors/text
            try:
                float_val = float(
                    str(row["Shr Out less Closely Held Sh"]).replace(",", "")
                )
                if not math.isnan(float_val):
                    FREE_FLOAT_DATA[ticker] = float_val
            except ValueError:
                continue

        logger.info(f"Loaded free float data for {len(FREE_FLOAT_DATA)} tickers.")
    except Exception as e:
        logger.error(f"Failed to load float data: {e}")


def load_json_mappings():
    global CUSIP_TO_CIK, CIK_TO_TICKER, TICKER_TO_CUSIP, CUSIP_TO_TICKER
    try:
        with open(CUSIP_TO_CIK_FILE_PATH, "r") as f:
            CUSIP_TO_CIK = json.load(f)
        with open(CIK_TO_TICKER_FILE_PATH, "r") as f:
            CIK_TO_TICKER = json.load(f)
        with open(TICKER_TO_CUSIP_FILE_PATH, "r") as f:
            TICKER_TO_CUSIP = json.load(f)

        # Build a safe reverse dictionary (CUSIP -> Ticker)
        # Because JSON values are lists, we iterate through them safely
        for ticker, cusip_list in TICKER_TO_CUSIP.items():
            if isinstance(cusip_list, list):
                for c in cusip_list:
                    CUSIP_TO_TICKER[c] = ticker
            elif isinstance(cusip_list, str):
                CUSIP_TO_TICKER[cusip_list] = ticker

        logger.info("Loaded JSON mappings for CUSIP<->CIK<->Ticker")
    except Exception as e:
        logger.error(f"Failed to load JSON mappings: {e}")


load_json_mappings()
load_float_data()

from contextlib import asynccontextmanager

# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------
APP_ENV = os.getenv("APP_ENV", "production").lower().strip()
DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes", "on")
ENABLE_DOCS = DEBUG or os.getenv("ENABLE_DOCS", "false").lower() in ("1", "true", "yes", "on")

INTERNAL_ERROR_DETAIL = "An internal error occurred. Please try again later."


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def validate_production_config() -> None:
    if APP_ENV == "production":
        _require_env("DB_PASSWORD")
    if not os.getenv("CORS_ORIGINS"):
        logger.warning("CORS_ORIGINS is not set; cross-origin requests will be blocked.")


DB_POOL_MIN_CONN = int(os.getenv("DB_POOL_MIN_CONN", "4"))
DB_POOL_MAX_CONN = int(os.getenv("DB_POOL_MAX_CONN", "20"))
db_pool: Optional[ThreadedConnectionPool] = None


def _get_db_connection_params() -> dict:
    if APP_ENV == "production":
        db_password = _require_env("DB_PASSWORD")
    else:
        db_password = os.getenv("DB_PASSWORD", "")

    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "database": os.getenv("DB_NAME", "sec"),
        "user": os.getenv("DB_USER", "postgres"),
        "password": db_password,
        "port": os.getenv("DB_PORT", "5432"),
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
        "sslmode": os.getenv("DB_SSLMODE", "prefer"),
        "keepalives": 1,
        "keepalives_idle": int(os.getenv("DB_KEEPALIVE_IDLE", "30")),
    }


def init_db_pool() -> None:
    """Initializes the ThreadedConnectionPool on application startup."""
    global db_pool
    if db_pool is not None:
        return
    try:
        params = _get_db_connection_params()
        db_pool = ThreadedConnectionPool(
            minconn=DB_POOL_MIN_CONN,
            maxconn=DB_POOL_MAX_CONN,
            **params,
        )
        logger.info(
            f"Initialized database connection pool (min={DB_POOL_MIN_CONN}, max={DB_POOL_MAX_CONN})"
        )
    except psycopg2.Error:
        logger.error("Failed to initialize database connection pool", exc_info=True)
        if APP_ENV == "production":
            raise


def close_db_pool() -> None:
    """Closes all connections in the pool on application shutdown."""
    global db_pool
    if db_pool is not None:
        try:
            db_pool.closeall()
            logger.info("Closed all connections in database pool")
        except Exception:
            logger.error("Error closing database connection pool", exc_info=True)
        finally:
            db_pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_production_config()
    logger.info("secapi starting")
    init_db_pool()
    yield
    logger.info("secapi shutting down")
    close_db_pool()


# Initialize FastAPI app and rate limiter
limiter = Limiter(key_func=get_remote_address, default_limits=["20/minute"])

app = FastAPI(
    title="SEC API",
    version="1.0.0",
    debug=DEBUG,
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# CORS configuration (explicit allowlist; empty list => no cross-origin requests)
origins_env = os.environ.get("CORS_ORIGINS", "").split(",")
allow_origins = [origin.strip() for origin in origins_env if origin.strip()]
logger.info(
    f"CORS allowed origins: {allow_origins if allow_origins else '<none> (cross-origin requests blocked)'}"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,  # Your frontend origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],  # Allow all headers
)

# Host-header validation. Only enforced when ALLOWED_HOSTS is configured.
allowed_hosts_env = os.getenv("ALLOWED_HOSTS", "")
allowed_hosts = [h.strip() for h in allowed_hosts_env.split(",") if h.strip()]
if allowed_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    logger.info(f"Trusted hosts: {allowed_hosts}")


# Baseline security headers on every response.
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-XSS-Protection", "0")
    return response


def get_db_connection():
    """
    Establishes and returns a new direct PostgreSQL database connection.
    Maintained for standalone operations and fallbacks.
    """
    try:
        params = _get_db_connection_params()
        return psycopg2.connect(**params)
    except psycopg2.Error:
        logger.error("Database connection failed", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


def get_db_cursor():
    """
    Dependency that yields a database cursor backed by a connection from the pool
    and ensures the connection is cleanly returned with rollback on error.
    """
    global db_pool
    conn = None
    cursor = None
    is_pooled = False

    # Attempt to initialize pool if not already initialized
    if db_pool is None:
        try:
            init_db_pool()
        except Exception:
            pass

    if db_pool is not None:
        try:
            conn = db_pool.getconn()
            is_pooled = True
        except PoolError:
            logger.error("Database connection pool exhausted", exc_info=True)
            raise HTTPException(
                status_code=503,
                detail="Database connection pool exhausted. Please retry shortly.",
            )
        except psycopg2.Error:
            logger.error("Failed to acquire connection from pool", exc_info=True)
            raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)
    else:
        # Fallback to direct connection if pool could not be initialized
        conn = get_db_connection()

    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        yield cursor
    except Exception:
        if conn and not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if cursor and not cursor.closed:
            try:
                cursor.close()
            except Exception:
                pass
        if conn:
            if is_pooled and db_pool is not None:
                if not conn.closed:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                try:
                    db_pool.putconn(conn)
                except Exception:
                    logger.error("Failed to return connection to pool", exc_info=True)
            else:
                try:
                    conn.close()
                except Exception:
                    pass


@app.get("/")
def read_root(request: Request):
    return {"message": "Welcome to the SEC API"}


@app.get("/health")
def health():
    """Liveness probe for load balancers and container healthchecks."""
    return {"status": "ok"}


def _format_address(
    street1: str,
    street2: str,
    city: str,
    state: str,
    state_desc: str,
    zipcode: str,
) -> str:
    """Format address components into a structured address"""
    # Clean empty values
    street1 = street1 or ""
    street2 = street2 or ""
    city = city or ""
    state = state or ""
    state_desc = state_desc or ""
    zipcode = zipcode or ""

    # Create full address string
    address_parts = []
    if street1:
        address_parts.append(street1)
    if street2:
        address_parts.append(street2)
    if city:
        address_parts.append(city)
    if state:
        address_parts.append(state)
    if state_desc:
        address_parts.append(state_desc)
    if zipcode:
        address_parts.append(zipcode)

    full_address = ", ".join(address_parts)

    return full_address


@app.get("/managers/", response_model=list[dict])
def get_managers(
    request: Request,
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """
    Retrieve all managers with pagination
    """
    query = """
            SELECT 
                cik_number,
                company_name, 
                company_phone,
                company_mail_street1,
                company_mail_street2,
                company_mail_city,
                company_mail_state,
                company_mail_state_desc,
                company_zipcode,
                company_business_street1,
                company_business_street2,
                company_business_city,
                company_business_state,
                company_business_state_desc,
                company_business_zipcode
            FROM companies
            LIMIT %s OFFSET %s
        """
    # Execute the query and fetch the results directly into a Pandas DataFrame
    logger.info(
        f"Executing query to fetch managers with limit={limit} and offset={offset}"
    )
    db.execute(query, (limit, offset))
    results = db.fetchall()

    companies = []
    for row in results:
        # Use dictionary keys from RealDictCursor for cleaner access
        mailing_address = _format_address(
            row.get("company_mail_street1"),
            row.get("company_mail_street2"),
            row.get("company_mail_city"),
            row.get("company_mail_state"),
            row.get("company_mail_state_desc"),
            row.get("company_mail_zipcode"),
        )
        business_address = _format_address(
            row.get("company_business_street1"),
            row.get("company_business_street2"),
            row.get("company_business_city"),
            row.get("company_business_state"),
            row.get("company_business_state_desc"),
            row.get("company_business_zipcode"),
        )

        companies.append(
            {
                "cik": str(row.get("cik_number")),
                "company_name": row.get("company_name"),
                "company_phone": row.get("company_phone"),
                "mailing_address": mailing_address,
                "business_address": business_address,
            }
        )

    logger.info(f"Fetched {len(companies)} managers from the database.")
    return companies


@app.get("/managers/{cik}", response_model=dict)
def get_manager(
    request: Request,
    cik: str,
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieve a single manager by CIK from the database
    """

    query = """
        SELECT
            cik_number,
            company_name, 
            company_phone,
            company_mail_street1,
            company_mail_street2,
            company_mail_city,
            company_mail_state,
            company_mail_state_desc,
            company_zipcode,
            company_business_street1,
            company_business_street2,
            company_business_city,
            company_business_state,
            company_business_state_desc,
            company_business_zipcode
        FROM companies
        WHERE cik_number = %s
    """

    try:
        logger.info(f"Executing query to fetch manager with CIK={cik}")
        db.execute(query, (cik,))
        result = db.fetchone()

        if not result:
            logger.error(f"Manager with CIK {cik} not found in the database.")
            raise HTTPException(
                status_code=404, detail=f"Manager with CIK {cik} not found"
            )

        mailing_address = _format_address(
            result.get("company_mail_street1"),
            result.get("company_mail_street2"),
            result.get("company_mail_city"),
            result.get("company_mail_state"),
            result.get("company_mail_state_desc"),
            result.get("company_mail_zipcode"),
        )
        business_address = _format_address(
            result.get("company_business_street1"),
            result.get("company_business_street2"),
            result.get("company_business_city"),
            result.get("company_business_state"),
            result.get("company_business_state_desc"),
            result.get("company_business_zipcode"),
        )

        manager_data = {
            "cik": str(result.get("cik_number")),
            "company_name": result.get("company_name"),
            "company_phone": result.get("company_phone"),
            "mailing_address": mailing_address,
            "business_address": business_address,
        }

        return manager_data
    except Exception as e:
        logger.error(f"Error fetching manager with CIK {cik}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@app.get("/managers/{cik}/filings")
def get_manager_filings(
    request: Request,
    cik: str,
    limit: int = Query(100, ge=1, le=1000, description="Number of results to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieve all filings for a specific manager by CIK number
    """

    try:
        logger.info(
            f"Fetching filings for manager with CIK={cik}, limit={limit}, offset={offset}"
        )
        db.execute("SELECT company_id FROM companies WHERE cik_number = %s", (cik,))
        company: dict = db.fetchone()  # type: ignore
        if not company:
            logger.error(f"Manager with CIK {cik} not found when fetching filings.")
            raise HTTPException(
                status_code=404, detail=f"Manager with CIK {cik} not found"
            )
        company_id = company["company_id"]  # type: ignore

        count_query = "SELECT COUNT(*) FROM filings WHERE company_id = %s"
        db.execute(count_query, (company_id,))
        total_count = db.fetchone()["count"]  # type: ignore

        if total_count == 0:
            logger.info(f"No filings found for manager with CIK={cik}")
            return {
                "filings": [],
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "total": 0,
                    "has_more": False,
                    "next_offset": None,
                },
            }

        filings_query = """
            SELECT
                accession_number,
                form_type,
                filing_date,
                period_of_report,
                file_number,
                filing_directory,
                created_at,
                updated_at
            FROM filings
            WHERE company_id = %s
            ORDER BY filing_date DESC
            LIMIT %s OFFSET %s
        """
        logger.info(
            f"Executing filings query for company_id={company_id} with limit={limit} and offset={offset}"
        )
        db.execute(filings_query, (company_id, limit, offset))
        filings_data = db.fetchall()

        # Format the response using the Pydantic model
        filings = [Filing(**row) for row in filings_data]  # type: ignore

        has_more = (offset + len(filings)) < total_count

        return {
            "filings": filings,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total_count,
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
            },
        }
    except HTTPException:
        logger.error(
            f"Error fetching filings for manager with CIK {cik}: Manager not found."
        )
        raise
    except Exception as e:
        logger.error(
            f"Error fetching filings for manager with CIK {cik}: {str(e)}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@app.get("/filings/", response_model=dict)
def get_filings(
    request: Request,
    limit: int = Query(100, description="Number of items to return", ge=1, le=100),
    offset: int = Query(0, description="Number of items to skip", ge=0),
    sort_by: str = Query("filing_date", description="Column to sort by"),
    sort_order: str = Query("desc", description="Sort order: 'asc' or 'desc'"),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    allowed_sort_columns = {
        "company_name": "c.company_name",
        "cik_number": "c.cik_number",
        "form_type": "f.form_type",
        "accession_number": "f.accession_number",
        "filing_date": "f.filing_date",
        "period_of_report": "f.period_of_report",
        "created_at": "f.created_at",
        "aum": "c.aum",
    }

    if sort_by not in allowed_sort_columns:
        logger.error(f"Invalid sort column specified: {sort_by}")
        raise HTTPException(status_code=400, detail="Invalid sort column specified.")

    # --- 2. Security: Validate sort order ---
    if sort_order.lower() not in ["asc", "desc"]:
        logger.error(f"Invalid sort order specified: {sort_order}")
        raise HTTPException(
            status_code=400, detail="Invalid sort order. Use 'asc' or 'desc'."
        )

    # Get the safe, validated column name and order
    sort_column = allowed_sort_columns[sort_by]
    sort_order_str = sort_order.upper()

    try:
        # Get the total count of filings for pagination metadata
        if sort_by == "aum":
            # Logic: If we sort by AUM, we only want the LATEST filing for each company.
            # Otherwise, we see the same company listed 20 times with the same AUM.

            # 1. Fast count of unique companies that have filings via index seek
            count_query = """
                SELECT COUNT(*) as count 
                FROM companies c 
                WHERE EXISTS (SELECT 1 FROM filings f WHERE f.company_id = c.company_id)
            """
            db.execute(count_query)
            total_count = db.fetchone()["count"]

            # 2. Query for Latest Filings sorted by Company AUM using index-driven lateral scan
            filings_query = f"""
                SELECT
                    lf.accession_number, lf.form_type, lf.filing_date, lf.period_of_report,
                    lf.file_number, lf.filing_directory, lf.created_at, lf.updated_at,
                    c.company_name, c.cik_number, c.aum
                FROM (
                    SELECT company_id, company_name, cik_number, aum
                    FROM companies c
                    WHERE EXISTS (SELECT 1 FROM filings f WHERE f.company_id = c.company_id)
                    ORDER BY NULLIF(aum, 0) {sort_order_str} NULLS LAST, company_name ASC
                    LIMIT %s OFFSET %s
                ) c
                CROSS JOIN LATERAL (
                    SELECT f.accession_number, f.form_type, f.filing_date, f.period_of_report,
                           f.file_number, f.filing_directory, f.created_at, f.updated_at
                    FROM filings f
                    WHERE f.company_id = c.company_id
                    ORDER BY f.filing_date DESC, f.accession_number DESC
                    LIMIT 1
                ) lf
            """
            logger.info(
                f"Executing filings query with AUM sorting, limit={limit}, offset={offset}"
            )
            db.execute(filings_query, (limit, offset))
            filings_data = db.fetchall()
        else:
            count_query = "SELECT COUNT(*) FROM filings"
            db.execute(count_query)
            total_count = db.fetchone()["count"]  # type: ignore

            order_by_clause = f"{sort_column} {sort_order_str}"
            if sort_by == "filing_date":
                order_by_clause += f", f.created_at {sort_order_str}"
            else:
                order_by_clause += ", f.filing_date DESC, f.created_at DESC"

            if total_count == 0:
                logger.info("No filings found in the database.")
                return {
                    "filings": [],
                    "pagination": {
                        "limit": limit,
                        "offset": offset,
                        "total": 0,
                        "has_more": False,
                    },
                    "sorting": {
                        "current_sort_by": sort_by,
                        "current_sort_order": sort_order,
                    },
                }

            filings_query = f"""
                SELECT
                    f.accession_number, f.form_type, f.filing_date, f.period_of_report,
                    f.file_number, f.filing_directory, f.created_at, f.updated_at,
                    c.company_name, c.cik_number, c.aum
                FROM filings f
                LEFT JOIN companies c ON f.company_id = c.company_id
                ORDER BY {order_by_clause}
                LIMIT %s OFFSET %s
            """

            db.execute(filings_query, (limit, offset))
            filings_data = db.fetchall()

        # Format the response using the Pydantic model
        filings = filings_data

        has_more = (offset + len(filings)) < total_count

        return {
            "filings": filings,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total_count,
                "has_more": has_more,
            },
            "sorting": {
                "current_sort_by": sort_by,
                "current_sort_order": sort_order,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching filings: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@app.get("/filings/{accession_number}", response_model=dict)
def get_filing_by_accession(
    request: Request,
    accession_number: str,
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):

    try:
        query = """
            SELECT
                f.accession_number,
                f.form_type,
                f.filing_date,
                f.period_of_report,
                f.file_number,
                f.filing_directory,
                f.created_at,
                f.updated_at,
                c.company_name,
                c.cik_number
            FROM filings f
            LEFT JOIN companies c ON f.company_id = c.company_id
            WHERE f.accession_number = %s
        """
        logger.info(
            "Executing query to fetch filing with accession number: %s",
            accession_number,
        )
        db.execute(query, (accession_number,))
        result = db.fetchone()

        if not result:
            logger.error(f"Filing with accession number {accession_number} not found.")
            raise HTTPException(
                status_code=404,
                detail=f"Filing with accession number {accession_number} not found",
            )

        return result  # type: ignore

    except Exception as e:
        logger.error(
            f"Error fetching filing with accession number {accession_number}: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@app.get("/holdings/{accession_number}", response_model=dict)
def get_holding_by_accession_number(
    request: Request,
    accession_number: str,
    # limit: int = Query(100, ge=1, le=1000),
    # offset: int = Query(0, ge=0),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
    draw: int = Query(0, ge=0, alias="draw"),
    start: int = Query(0, ge=0, alias="start"),
    length: int = Query(10, ge=-1, le=1000, alias="length"),
    search_value: Optional[str] = Query(None, alias="search[value]"),
    order_column_index: int = Query(0, alias="order[0][column]"),
    order_dir: str = Query("asc", alias="order[0][dir]"),
):
    """
    Retrieve a single holding by accession number
    """

    try:
        # Map DataTables column index to actual database column names
        column_map = {
            0: "i.issuer_name",
            1: "i.cusip",
            2: "t.name",
            3: "h.value",
            4: "h.shares_or_principal_amount",
            5: "s.name",
            6: "d.name",
            7: "p.name",
            8: "h.voting_authority_sole",
            9: "h.voting_authority_shared",
            10: "h.voting_authority_none",
        }
        order_by_column = column_map.get(order_column_index, "h.value")
        order_direction = "DESC" if order_dir == "desc" else "ASC"

        # DataTables sends length=-1 for "Show all"; map it to a safe internal cap.
        effective_length = 5000 if length == -1 else length

        # Base query to get the total count of all records (before any filtering)
        count_query = """
            SELECT COUNT(*)
            FROM holdings h
            INNER JOIN filings f ON h.filing_id = f.filing_id
            WHERE f.accession_number = %s;
        """
        db.execute(count_query, (accession_number,))
        total_records = db.fetchone()["count"]  # type: ignore

        # Construct the WHERE clause for searching
        where_clause = "WHERE f.accession_number = %s"
        search_params = [accession_number]
        if search_value:
            where_clause += """
                AND (
                    i.issuer_name ILIKE %s OR
                    i.cusip ILIKE %s OR
                    t.name ILIKE %s
                )
            """
            search_pattern = f"%{search_value}%"
            search_params.extend([search_pattern, search_pattern, search_pattern])

        # Query to get the count after applying the search filter
        filtered_count_query = f"""
            SELECT COUNT(*)
            FROM holdings h
            INNER JOIN filings f ON h.filing_id = f.filing_id
            LEFT JOIN issuers i ON h.issuer_id = i.issuer_id
            LEFT JOIN title_of_class_table t ON h.title_of_class = t.id
            {where_clause};
        """
        db.execute(filtered_count_query, search_params)
        filtered_records = db.fetchone()["count"]

        # Main query to fetch the paginated, filtered, and sorted data
        holdings_query = f"""
            SELECT
                h.holding_id,
                i.issuer_name,
                t.name AS title_of_class,
                h.shares_or_principal_amount,
                s.name AS shares_or_principal_type,
                h.value,
                p.name AS put_or_call,
                d.name AS investment_discretion,
                h.voting_authority_sole,
                h.voting_authority_shared,
                h.voting_authority_none,
                i.cusip
            FROM holdings h
            INNER JOIN filings f ON h.filing_id = f.filing_id
            LEFT JOIN issuers i ON h.issuer_id = i.issuer_id
            LEFT JOIN title_of_class_table t ON h.title_of_class = t.id
            LEFT JOIN share_type_table s ON h.shares_or_principal_type = s.id
            LEFT JOIN put_or_call_table p ON h.put_or_call = p.id
            LEFT JOIN investment_discretion_table d ON h.investment_discretion = d.id
            {where_clause}
            ORDER BY {order_by_column} {order_direction}
            OFFSET %s LIMIT %s;
        """
        query_params = search_params + [start, effective_length]
        logger.info(
            f"Executing holdings query for accession_number={accession_number} with search='{search_value}', order_by='{order_by_column} {order_direction}', start={start}, length={length}"
        )
        db.execute(holdings_query, query_params)
        holdings_data = db.fetchall()

        formatted_data = [
            {
                "issuer_name": row.get("issuer_name"),
                "cusip": row.get("cusip"),
                "title_of_class": row.get("title_of_class"),
                "value": row.get("value"),
                "shares_or_principal_amount": row.get("shares_or_principal_amount"),
                "shares_or_principal_type": row.get("shares_or_principal_type"),
                "investment_discretion": row.get("investment_discretion"),
                "put_or_call": row.get("put_or_call"),
                "voting_authority_sole": row.get("voting_authority_sole"),
                "voting_authority_shared": row.get("voting_authority_shared"),
                "voting_authority_none": row.get("voting_authority_none"),
            }
            for row in holdings_data
        ]

        # Format the response for DataTables
        return {
            "draw": draw,
            "recordsTotal": total_records,
            "recordsFiltered": filtered_records,
            "data": formatted_data,
        }

    except HTTPException:
        logger.error(f"Holdings for accession number {accession_number} not found.")
        raise HTTPException(
            status_code=404,
            detail=f"Holdings for accession number {accession_number} not found",
        )
    except Exception as e:
        logger.error(
            f"Error fetching holdings for accession number {accession_number}: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@app.post("/api/ai_summary")
@limiter.limit("20/minute")
async def openai_call(
    request: Request,
    payload: HoldingsRequest = Body(...),
):

    if not DEEPSEEK_API_KEY:
        logger.error("DEEPSEEK_API_KEY is not configured. Cannot call OpenAI API.")
        return JSONResponse(
            content={"summary": "API key not configured"}, status_code=503
        )

    # Frontend POST request will be in dict format?
    new_holdings = pl.DataFrame(payload.new_holdings)
    closed_positions = pl.DataFrame(payload.closed_positions)
    increased_holdings = pl.DataFrame(payload.increased_holdings)
    decreased_holdings = pl.DataFrame(payload.decreased_holdings)

    new_holdings_top_5 = pl.DataFrame()
    closed_positions_top_5 = pl.DataFrame()
    increased_holdings_top_5 = pl.DataFrame()
    decreased_holdings_top_5 = pl.DataFrame()

    if not new_holdings.is_empty():
        new_holdings_top_5 = new_holdings.sort(
            ["total_value_latest", "total_shares_latest"],
            descending=True,
        ).head(5)

    if not closed_positions.is_empty():
        closed_positions_top_5 = closed_positions.sort(
            ["total_value_prev", "total_shares_prev"], descending=True
        ).head(5)

    if not increased_holdings.is_empty():
        increased_holdings_top_5 = increased_holdings.sort(
            ["change_in_share", "percent_change"], descending=True
        ).head(5)

    if not decreased_holdings.is_empty():
        decreased_holdings_top_5 = decreased_holdings.sort(
            [
                "change_in_share",
                "percent_change",
            ],
            descending=False,
        ).head(5)

    # preparation to call openai API
    new_holdings_dict = {
        row["issuer_name_clean"]: row["total_shares_latest"]
        for row in new_holdings_top_5.to_dicts()
    }
    closed_positions_dict = {
        row["issuer_name_clean_prev"]: row["total_shares_prev"]
        for row in closed_positions_top_5.to_dicts()
    }
    increased_holdings_dict = {
        row["issuer_name_clean"]: {
            k: v
            for k, v in row.items()
            if k not in ["issuer_name_clean", "issuer_name_clean_prev"]
        }
        for row in increased_holdings_top_5.to_dicts()
    }
    decreased_holdings_dict = {
        row["issuer_name_clean"]: {
            k: v
            for k, v in row.items()
            if k not in ["issuer_name_clean", "issuer_name_clean_prev"]
        }
        for row in decreased_holdings_top_5.to_dicts()
    }

    new_holding_text = "|".join([f"{k} {v}" for k, v in new_holdings_dict.items()])
    closed_positions_text = "|".join(
        [f"{k} {v}" for k, v in closed_positions_dict.items()]
    )
    increased_holdings_text = "|".join(
        [f"{k} {v}" for k, v in increased_holdings_dict.items()]
    )
    decreased_holdings_text = "|".join(
        [f"{k} {v}" for k, v in decreased_holdings_dict.items()]
    )

    async def _get_summary(title, data_text):
        prompt = f"""
        Generate a summary of the holdings changes for the fund management in one or two sentences.
        {title}: {data_text}
        """
        # Assuming an async OpenAI client
        response = await client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {
                    "role": "system",
                    "content": "You are a professional financial analyst. Be concise.",
                },
                {"role": "user", "content": prompt},
            ],
            stream=False,
        )
        return response.choices[0].message.content

    api_calls = [
        _get_summary("New Holdings", new_holding_text),
        _get_summary("Closed Positions", closed_positions_text),
        _get_summary("Increased Holdings", increased_holdings_text),
        _get_summary("Decreased Holdings", decreased_holdings_text),
    ]
    try:
        texts = await asyncio.gather(*api_calls)

        final_summary = " ".join([text for text in texts if text])
        return JSONResponse(content={"summary": final_summary})
    except Exception as e:
        logger.error(f"Error calling OpenAI API: {e}", exc_info=True)
        return JSONResponse(
            content={
                "summary": "AI summary is currently unavailable due to a provider error."
            },
            status_code=502,
        )


def _df_to_dict_list(df: pl.DataFrame):
    """Convert DataFrame to list of dictionaries"""
    if df.is_empty():
        return []
    return df.to_dicts()


FILING_QUERY_WITH_PRIORITY = """
    SELECT
        f.accession_number,
        c.cik_number,
        c.company_name,
        f.form_type,
        f.filing_date,
        f.period_of_report,
        f.filing_directory
    FROM filings f
    JOIN companies c ON f.company_id = c.company_id
    WHERE
        c.cik_number = (SELECT c2.cik_number FROM filings f2 JOIN companies c2 ON f2.company_id = c2.company_id WHERE f2.accession_number = %s LIMIT 1)
    AND
        f.period_of_report = (SELECT f3.period_of_report FROM filings f3 WHERE f3.accession_number = %s LIMIT 1)
    ORDER BY
    CASE
        WHEN f.form_type = '13F-HR/A/A' THEN 1
        WHEN f.form_type = '13F-HR/A' THEN 2
        WHEN f.form_type = '13F-HR' THEN 3
        ELSE 4
    END,
    f.filing_date DESC
    LIMIT 1
"""

HOLDINGS_QUERY = """
    SELECT
        h.holding_id,
        i.issuer_name,
        t.name AS title_of_class,
        COALESCE(t.is_common_stock, FALSE) AS is_common_stock,
        h.shares_or_principal_amount,
        s.name AS shares_or_principal_type,
        h.value,
        p.name AS put_or_call,
        d.name AS investment_discretion,
        h.voting_authority_sole,
        h.voting_authority_shared,
        h.voting_authority_none,
        i.cusip,
        i.sic
    FROM holdings h
    LEFT JOIN issuers i ON h.issuer_id = i.issuer_id
    LEFT JOIN title_of_class_table t ON h.title_of_class = t.id
    LEFT JOIN share_type_table s ON h.shares_or_principal_type = s.id
    LEFT JOIN put_or_call_table p ON h.put_or_call = p.id
    LEFT JOIN investment_discretion_table d ON h.investment_discretion = d.id
    WHERE h.filing_id = (SELECT filing_id FROM filings WHERE accession_number = %s)
    ORDER BY h.value DESC
"""

HOLDINGS_SCHEMA = {
    "holding_id": pl.Int64,
    "issuer_name": pl.Utf8,
    "title_of_class": pl.Utf8,
    "is_common_stock": pl.Boolean,
    "shares_or_principal_amount": pl.Int64,
    "shares_or_principal_type": pl.Utf8,
    "value": pl.Int64,
    "put_or_call": pl.Utf8,
    "investment_discretion": pl.Utf8,
    "voting_authority_sole": pl.Int64,
    "voting_authority_shared": pl.Int64,
    "voting_authority_none": pl.Int64,
    "cusip": pl.Utf8,
    "sic": pl.Int64,
}


def _process_filings(
    df: pl.DataFrame,
    latest=True,
):
    suffix = "latest" if latest else "prev"

    if df.is_empty():
        logger.warning(
            f"No holdings data found for {'latest' if latest else 'previous'} filing."
        )
        output_schema = {
            "cusip": pl.Utf8,
            "put_or_call": pl.Utf8,
            "issuer_name_clean": pl.Utf8,
            "sic": pl.Int64,
            f"total_shares_{suffix}": pl.Int64,
            f"total_value_{suffix}": pl.Int64,
            f"per_share_price_{suffix}": pl.Float64,
        }
        return pl.DataFrame([], schema=output_schema), False

    df_with_prices = df.with_columns(
        (pl.col("value") / pl.col("shares_or_principal_amount")).alias("raw_price")
    )
    median_price = df_with_prices.filter(pl.col("raw_price") > 0)["raw_price"].median()

    requires_multiplication = False
    if median_price is not None and median_price < 2.0:  # Threshold of $2.00
        requires_multiplication = True

    updated_df = (
        df_with_prices.filter(pl.col("is_common_stock"))
        .with_columns(
            pl.col("issuer_name")
            .str.strip_chars()
            .str.to_uppercase()
            .alias("issuer_name_clean")
        )
        .with_columns(
            pl.when(pl.lit(requires_multiplication))
            .then(pl.col("value") * 1000)
            .otherwise(pl.col("value"))
            .alias("corrected_value")
        )
        .group_by(["cusip", "put_or_call"])
        .agg(
            pl.col("issuer_name_clean").first().alias("issuer_name_clean"),
            pl.col("sic").first().alias("sic"),
            pl.col("shares_or_principal_amount").sum().alias(f"total_shares_{suffix}"),
            pl.col("corrected_value").sum().alias(f"total_value_{suffix}"),
        )
        .with_columns(
            pl.when(pl.col(f"total_shares_{suffix}") > 0)
            .then(pl.col(f"total_value_{suffix}") / pl.col(f"total_shares_{suffix}"))
            .otherwise(pl.lit(0))
            .round(2)
            .alias(f"per_share_price_{suffix}")
        )
    )

    return updated_df, requires_multiplication


@app.get("/analysis/{previous_accession}/{latest_accession}", response_model=dict)
def compare_holdings(
    request: Request,
    previous_accession: str,
    latest_accession: str,
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Compare two holdings by their accession numbers
    """

    try:

        acc_latest = latest_accession.strip()
        acc_prev = previous_accession.strip()

        # Get filing details for the previous and latest filings
        db.execute(FILING_QUERY_WITH_PRIORITY, (acc_prev, acc_prev))
        previous_filing = db.fetchone()

        db.execute(FILING_QUERY_WITH_PRIORITY, (acc_latest, acc_latest))
        latest_filing = db.fetchone()

        if not previous_filing:
            logger.error(
                "Previous filing with accession number %s not found.", acc_prev
            )
            raise HTTPException(
                status_code=404, detail=f"Previous filing {acc_prev} not found"
            )
        if not latest_filing:
            logger.error(
                "Latest filing with accession number %s not found.", acc_latest
            )
            raise HTTPException(
                status_code=404, detail=f"Latest filing {acc_latest} not found"
            )

        latest_acc = latest_filing["accession_number"]  # type: ignore
        previous_acc = previous_filing["accession_number"]  # type: ignore

        if previous_filing["cik_number"] != latest_filing["cik_number"]:
            logger.error(
                "CIK mismatch: Previous filing CIK %s does not match Latest filing CIK %s",
                previous_filing["cik_number"],
                latest_filing["cik_number"],
            )
            return {"error": "CIK for latest and previous quarters do not match"}

        # Ensure correct ordering based on filing dates
        if previous_filing["period_of_report"] > latest_filing["period_of_report"]:
            previous_filing, latest_filing = latest_filing, previous_filing
            acc_prev, acc_latest = (
                previous_acc,
                latest_acc,
            )
        amendment_used = None
        message_parts = []
        if latest_acc != acc_latest:
            message_parts.append(
                f"The latest filing ({acc_latest}) was replaced by its amendment ({latest_acc}) for the comparison."
            )

        if previous_acc != acc_prev:
            message_parts.append(
                f"The previous filing ({acc_prev}) was replaced by its amendment ({previous_acc}) for the comparison."
            )

        if message_parts:
            amendment_used = " ".join(message_parts)

        api_save_comparison(
            previous_acc,
            latest_acc,
            db,
        )

        # Fetch holdings data from the database
        db.execute(HOLDINGS_QUERY, (acc_prev,))
        previous_holdings_data = db.fetchall()

        db.execute(HOLDINGS_QUERY, (acc_latest,))
        latest_holdings_data = db.fetchall()

        # Convert to Polars DataFrames for efficient analysis
        if not previous_holdings_data:
            previous_df = pl.DataFrame([], schema=HOLDINGS_SCHEMA)
        else:
            previous_df = pl.DataFrame(previous_holdings_data, schema=HOLDINGS_SCHEMA)

        if not latest_holdings_data:
            latest_df = pl.DataFrame([], schema=HOLDINGS_SCHEMA)
        else:
            latest_df = pl.DataFrame(latest_holdings_data, schema=HOLDINGS_SCHEMA)

        # Exclude derivative option (Put/Call) positions from the comparison entirely.
        # This is applied at the source so options never appear in the common-stock
        # bucket either (option rows in 13F data often carry a 'COM'-style title).
        _is_option_row = pl.col("put_or_call").is_not_null() & pl.col(
            "put_or_call"
        ).str.to_uppercase().is_in(OPTION_PUT_CALL_NAMES)
        previous_df = previous_df.filter(~_is_option_row)
        latest_df = latest_df.filter(~_is_option_row)

        # Data cleaning and aggregation + filter ONLY COMMON STOCK
        latest_aggregated, latest_multiplication = _process_filings(latest_df)
        prev_aggregated, prev_multiplication = _process_filings(
            previous_df,
            latest=False,
        )

        # other securities (non-common stock)
        latest_other_securities = latest_df.filter(~pl.col("is_common_stock"))
        prev_other_securities = previous_df.filter(~pl.col("is_common_stock"))

        # Data cleaning and aggregation for latest other securities
        latest_other_aggregated = (
            latest_other_securities.with_columns(
                pl.col("issuer_name")
                .str.strip_chars()
                .str.to_uppercase()
                .alias("issuer_name_clean")
            )
            .group_by(
                ["cusip", "put_or_call"]
            )  # GROUP BY CUSIP RATHER THAN ISSUER NAME
            .agg(
                # Renaming the column to avoid confusion with common stock shares
                pl.col("issuer_name_clean").first().alias("issuer_name_clean"),
                pl.col("shares_or_principal_amount").sum().alias("total_units_latest"),
                pl.col("value").sum().alias("total_value_latest"),
            )
        )

        # Data cleaning and aggregation for previous other securities
        prev_other_aggregated = (
            prev_other_securities.with_columns(
                pl.col("issuer_name")
                .str.strip_chars()
                .str.to_uppercase()
                .alias("issuer_name_clean")
            )
            .group_by(["cusip", "put_or_call"])
            .agg(
                # Renaming the column to avoid confusion with common stock shares
                pl.col("issuer_name_clean").first().alias("issuer_name_clean"),
                pl.col("shares_or_principal_amount").sum().alias("total_units_prev"),
                pl.col("value").sum().alias("total_value_prev"),
            )
        )

        # join
        merged_df = latest_aggregated.join(
            prev_aggregated, on="cusip", how="full", suffix="_prev"
        )
        merged_other_df = latest_other_aggregated.join(
            prev_other_aggregated, on="cusip", how="full", suffix="_prev"
        )

        if not CUSIP_DETAILS_DF.is_empty():
            if "sic" in merged_df.columns:
                merged_df = merged_df.drop(["sic", "sic_prev"])

            # Join the enriched CSV data
            merged_df = merged_df.join(CUSIP_DETAILS_DF, on="cusip", how="left")
            merged_other_df = merged_other_df.join(
                CUSIP_DETAILS_DF, on="cusip", how="left"
            )

        #### SECTOR ANALYSIS ####
        sector_changes = (
            merged_df.filter(pl.col("sicSector").is_not_null())
            .group_by("sicSector")
            .agg(
                pl.col("total_value_latest")
                .fill_null(0)
                .sum()
                .alias("latest_sector_total"),
                pl.col("total_value_prev")
                .fill_null(0)
                .sum()
                .alias("prev_sector_total"),
            )
            .with_columns(
                # Calculate percentage change
                pl.when(pl.col("prev_sector_total") > 0)
                .then(
                    (
                        (pl.col("latest_sector_total") - pl.col("prev_sector_total"))
                        / pl.col("prev_sector_total")
                    )
                    * 100
                )
                # If prev total was 0, change is infinite/new; set to None
                .otherwise(None)
                .round(2)
                .alias("percent_change")
            )
        )

        increased_sectors = sector_changes.filter(
            (pl.col("percent_change") > 0) | (pl.col("prev_sector_total") == 0)
        ).sort("percent_change", descending=True)

        decreased_sectors = sector_changes.filter(pl.col("percent_change") < 0).sort(
            "percent_change", descending=False
        )

        # 1. Changes grouped by Industry
        industry_changes = (
            merged_df.filter(pl.col("industry").is_not_null())
            .group_by("industry")
            .agg(
                pl.col("total_value_latest").fill_null(0).sum().alias("latest_total"),
                pl.col("total_value_prev").fill_null(0).sum().alias("prev_total"),
            )
            .with_columns(
                pl.when(pl.col("prev_total") > 0)
                .then(
                    (
                        (pl.col("latest_total") - pl.col("prev_total"))
                        / pl.col("prev_total")
                    )
                    * 100
                )
                .otherwise(None)
                .round(2)
                .alias("percent_change")
            )
        )

        inc_industries = industry_changes.filter(
            (pl.col("percent_change") > 0) | (pl.col("prev_total") == 0)
        ).sort("percent_change", descending=True, nulls_last=False)

        dec_industries = industry_changes.filter(pl.col("percent_change") < 0).sort(
            "percent_change", descending=False
        )

        # 2. Changes grouped by SIC Code
        sic_changes = (
            merged_df.filter(pl.col("sic").is_not_null())
            .group_by("sic")
            .agg(
                pl.col("total_value_latest").fill_null(0).sum().alias("latest_total"),
                pl.col("total_value_prev").fill_null(0).sum().alias("prev_total"),
            )
            .with_columns(
                pl.when(pl.col("prev_total") > 0)
                .then(
                    (
                        (pl.col("latest_total") - pl.col("prev_total"))
                        / pl.col("prev_total")
                    )
                    * 100
                )
                .otherwise(None)
                .round(2)
                .alias("percent_change")
            )
        )

        inc_sics = sic_changes.filter(
            (pl.col("percent_change") > 0) | (pl.col("prev_total") == 0)
        ).sort("percent_change", descending=True, nulls_last=False)

        dec_sics = sic_changes.filter(pl.col("percent_change") < 0).sort(
            "percent_change", descending=False
        )

        ### SECTOR ANALYSIS END ###

        # if previously no shares, now has shares -> new holding
        new_holdings = merged_df.filter(pl.col("total_shares_prev").is_null()).select(
            "issuer_name_clean",
            "total_shares_latest",
            "total_value_latest",
            "per_share_price_latest",
            "put_or_call",
            "cusip",
        )
        closed_positions = merged_df.filter(
            pl.col("total_shares_latest").is_null()
        ).select(
            "issuer_name_clean_prev",
            "total_shares_prev",
            "total_value_prev",
            "per_share_price_prev",
            "put_or_call",
            "cusip_prev",
        )
        new_other_holdings = merged_other_df.filter(
            pl.col("total_units_prev").is_null()
        ).select(
            "issuer_name_clean",
            "total_units_latest",
            "total_value_latest",
            "put_or_call",
            "cusip",
        )
        closed_other_positions = merged_other_df.filter(
            pl.col("total_units_latest").is_null()
        ).select(
            "issuer_name_clean_prev",
            "total_units_prev",
            "total_value_prev",
            "put_or_call",
            "cusip_prev",
        )

        # Common holdings with changes (increase or decreases)
        common_holdings = (
            merged_df.filter(
                pl.col("total_shares_prev").is_not_null()
                & pl.col("total_shares_latest").is_not_null()
            )
            .with_columns(
                [
                    (pl.col("total_shares_latest") - pl.col("total_shares_prev")).alias(
                        "change_in_share"
                    ),
                    (
                        (pl.col("total_shares_latest") - pl.col("total_shares_prev"))
                        / pl.col("total_shares_prev")
                        * 100
                    )
                    .round(2)
                    .alias("percent_change"),
                ]
            )
            .with_columns(
                pl.when(pl.col("percent_change").is_nan())
                .then(None)
                .when(pl.col("percent_change").is_infinite())
                .then(None)
                .otherwise(pl.col("percent_change"))
                .alias("percent_change")
            )
            .select(
                "issuer_name_clean",
                "total_shares_prev",
                "total_value_prev",
                "per_share_price_prev",
                "total_shares_latest",
                "total_value_latest",
                "per_share_price_latest",
                "change_in_share",
                "percent_change",
                "put_or_call",
                "cusip",
            )
        )

        # Increases
        increased_holdings = common_holdings.filter(pl.col("change_in_share") > 0)
        decreased_holdings = common_holdings.filter(pl.col("change_in_share") < 0)
        unchanged_holdings = common_holdings.filter(pl.col("change_in_share") == 0)

        # other securities with changes (increase or decreases)
        common_other_holdings = (
            merged_other_df.filter(
                pl.col("total_units_prev").is_not_null()
                & pl.col("total_units_latest").is_not_null()
            )
            .with_columns(
                [
                    (pl.col("total_units_latest") - pl.col("total_units_prev")).alias(
                        "change_in_units"
                    ),
                    (
                        (pl.col("total_units_latest") - pl.col("total_units_prev"))
                        / pl.col("total_units_prev")
                        * 100
                    )
                    .round(2)
                    .alias("percent_change"),
                ]
            )
            .with_columns(
                pl.when(pl.col("percent_change").is_nan())
                .then(None)
                .when(pl.col("percent_change").is_infinite())
                .then(None)
                .otherwise(pl.col("percent_change"))
                .alias("percent_change")
            )
            .select(
                "issuer_name_clean",
                "total_units_prev",
                "total_value_prev",
                "total_units_latest",
                "total_value_latest",
                "change_in_units",
                "percent_change",
                "put_or_call",
                "cusip",
            )
        )

        # Increases
        increased_other_holdings = common_other_holdings.filter(
            pl.col("change_in_units") > 0
        )
        decreased_other_holdings = common_other_holdings.filter(
            pl.col("change_in_units") < 0
        )
        unchanged_other_holdings = common_other_holdings.filter(
            pl.col("change_in_units") == 0
        )

        top_5_holdings_other_by_value = latest_other_aggregated.sort(
            by="total_value_latest", descending=True
        ).head(5)
        top_5_holdings_by_value = latest_aggregated.sort(
            by="total_value_latest", descending=True
        ).head(5)
        top_5_new_common = new_holdings.sort(
            by="total_value_latest", descending=True
        ).head(5)
        top_5_closed_common = closed_positions.sort(
            by="total_value_prev", descending=True
        ).head(5)
        top_5_increased_common = increased_holdings.sort(
            by="percent_change", descending=True
        ).head(5)
        top_5_decreased_common = decreased_holdings.sort(by="percent_change").head(5)

        def inject_tickers(data_list, cusip_key="cusip"):
            if not data_list:
                return []
            for item in data_list:
                val = item.get(cusip_key)
                item["ticker"] = CUSIP_TO_TICKER.get(val)
            return data_list

        # Prepare response data
        response_data = {
            "metadata": {
                "cik": latest_filing.get("cik_number"),
                "company_name": latest_filing.get("company_name"),
                "ai_summary": "Not Available",
                "amendment_used": amendment_used,
                "latest_filing": {
                    "accession_number": latest_filing.get("accession_number"),
                    "filing_date": (
                        latest_filing.get("filing_date").isoformat()
                        if latest_filing.get("filing_date")
                        else None
                    ),
                    "period_of_report": latest_filing.get("period_of_report"),
                    "form_type": latest_filing.get("form_type"),
                    "user_input": acc_latest,
                    "filing_directory": latest_filing.get("filing_directory"),
                    "multiplication_applied": latest_multiplication,
                },
                "previous_filing": {
                    "accession_number": previous_filing.get("accession_number"),
                    "filing_date": (
                        previous_filing.get("filing_date").isoformat()
                        if previous_filing.get("filing_date")
                        else None
                    ),
                    "period_of_report": previous_filing.get("period_of_report"),
                    "form_type": previous_filing.get("form_type"),
                    "user_input": acc_prev,
                    "filing_directory": previous_filing.get("filing_directory"),
                    "multiplication_applied": prev_multiplication,
                },
                "summary": {
                    "total_companies_latest": latest_aggregated.height,
                    "total_companies_previous": prev_aggregated.height,
                    "new_holdings_count": new_holdings.height,
                    "closed_positions_count": closed_positions.height,
                    "increased_holdings_count": increased_holdings.height,
                    "decreased_holdings_count": decreased_holdings.height,
                    "unchanged_holdings_count": unchanged_holdings.height,
                    "sector_changes": {
                        "by_sector": {
                            "increased": increased_sectors.to_dicts(),
                            "decreased": decreased_sectors.to_dicts(),
                        },
                        "by_industry": {
                            "increased": inc_industries.to_dicts(),
                            "decreased": dec_industries.to_dicts(),
                        },
                        "by_sic": {
                            "increased": inc_sics.to_dicts(),
                            "decreased": dec_sics.to_dicts(),
                        },
                    },
                },
            },
            "holdings": {
                "top_holdings_by_value": inject_tickers(
                    top_5_holdings_by_value.to_dicts()
                ),
                "top_other_securities_by_value": inject_tickers(
                    top_5_holdings_other_by_value.to_dicts()
                ),
                "new_holdings": {
                    "top_5": inject_tickers(top_5_new_common.to_dicts()),
                    "common_stock": inject_tickers(_df_to_dict_list(new_holdings)),
                    "other_securities": inject_tickers(
                        _df_to_dict_list(new_other_holdings)
                    ),
                },
                "closed_positions": {
                    "top_5": inject_tickers(
                        top_5_closed_common.to_dicts(), "cusip_prev"
                    ),
                    "common_stock": inject_tickers(
                        _df_to_dict_list(closed_positions), "cusip_prev"
                    ),
                    "other_securities": inject_tickers(
                        _df_to_dict_list(closed_other_positions), "cusip_prev"
                    ),
                },
                "increased_holdings": {
                    "top_5": inject_tickers(top_5_increased_common.to_dicts()),
                    "common_stock": inject_tickers(
                        _df_to_dict_list(increased_holdings)
                    ),
                    "other_securities": inject_tickers(
                        _df_to_dict_list(increased_other_holdings)
                    ),
                },
                "decreased_holdings": {
                    "top_5": inject_tickers(top_5_decreased_common.to_dicts()),
                    "common_stock": inject_tickers(
                        _df_to_dict_list(decreased_holdings)
                    ),
                    "other_securities": inject_tickers(
                        _df_to_dict_list(decreased_other_holdings)
                    ),
                },
                "common_holdings": {
                    "common_stock": inject_tickers(
                        _df_to_dict_list(unchanged_holdings)
                    ),
                    "other_securities": inject_tickers(
                        _df_to_dict_list(unchanged_other_holdings)
                    ),
                },
            },
        }

        return response_data

    except HTTPException as e:
        logger.error(f"HTTPException occurred: {e.detail}")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@app.get("/comparisons", response_model=list)
def get_recent_comparisons(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    try:
        query = """
            SELECT accession_number_1, accession_number_2, created_at
            FROM recent_comparisons
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """
        logger.info(
            f"Fetching recent comparisons with limit {limit} and offset {offset}"
        )
        db.execute(query, (limit, offset))
        comparisons = db.fetchall()
        return comparisons
    except Exception as e:
        logger.error(f"Error fetching recent comparisons: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


class ComparisonRequest(BaseModel):
    acc_num_1: str
    acc_num_2: str


@app.post("/comparisons")
def save_comparison(
    request: Request,
    comparison_request: ComparisonRequest,
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    try:
        query = """
            INSERT INTO recent_comparisons (accession_number_1, accession_number_2)
            VALUES (%s, %s)
        """
        db.execute(query, (comparison_request.acc_num_1, comparison_request.acc_num_2))
        db.connection.commit()

        return {"message": "Comparison saved successfully"}
    except Exception as e:
        logger.error(f"Error saving comparison: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


def api_save_comparison(accession_1: str, accession_2: str, db):
    try:
        query = """
            INSERT INTO recent_comparisons (accession_number_1, accession_number_2)
            VALUES (%s, %s)
        """
        db.execute(query, (accession_1, accession_2))
        db.connection.commit()

        return {"message": "Comparison saved successfully"}
    except Exception as e:
        logger.error(f"Error saving comparison: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@app.get("/company/{cik}/compare/latest")
def compare_latest_filings(
    request: Request,
    cik: str,
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Automatically compares the latest two quarterly filings for a given company CIK.
    """
    try:
        # Step 1: Find the latest two filings for the given CIK
        filing_query = """
            SELECT
                accession_number,
                period_of_report
            FROM (
                SELECT
                    f.accession_number,
                    f.period_of_report,
                    ROW_NUMBER() OVER(PARTITION BY f.period_of_report ORDER BY f.filing_date DESC, f.accession_number DESC) as rn
                FROM filings f
                INNER JOIN companies c ON f.company_id = c.company_id
                WHERE c.cik_number = %s AND f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
            ) AS ranked_filings
            WHERE rn = 1
            ORDER BY period_of_report desc
            LIMIT 2;
        """
        db.execute(filing_query, (cik,))
        filings = db.fetchall()

        if len(filings) < 2:
            raise HTTPException(
                status_code=404,
                detail=f"Not enough filings found for CIK {cik} to perform a comparison.",
            )

        latest_filing = filings[0]["accession_number"]
        previous_filing = filings[1]["accession_number"]

        logger.info(
            f"Comparing filings {previous_filing} and {latest_filing} for CIK {cik}"
        )

        # Step 2: Use the existing compare_holdings logic to perform the comparison
        # You'll need to call the function directly here
        return compare_holdings(
            request=request,
            previous_accession=previous_filing,
            latest_accession=latest_filing,
            db=db,
        )

    except HTTPException:
        raise  # Re-raise HTTPException to be handled by FastAPI
    except Exception as e:
        logger.error(
            f"Error comparing latest filings for CIK {cik}: {str(e)}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail=INTERNAL_ERROR_DETAIL
        )


class SignificantHolding(BaseModel):
    """Represents a single significant holding for the story."""

    issuer_name: str
    cusip: str | None = None
    ticker: Optional[str] = None
    shares_or_principal_amount: int
    value: int
    # The 'change_type' can be 'new', 'increase', 'decrease', etc.
    change_type: str
    price_per_share: float | None = None


class HoldingChange(BaseModel):
    issuer_name: str
    cusip: Optional[str] = None
    ticker: Optional[str] = None
    shares_or_principal_amount: int  # The new, current amount of shares
    change_in_share: int
    percent_change: Optional[float] = (
        None  # Use float for percentage, optional for safety
    )
    price_per_share: Optional[float] = None
    price_per_unit: Optional[float] = None
    change_type: str


class StorySummary(BaseModel):
    """A summary of a single filing's story for a list view."""

    cik: str
    aum: int | None = None
    latest_accession_number: str
    previous_accession_number: str
    company_name: str
    reporting_period: dt.date
    filing_date: dt.date
    top_new_position: Optional[SignificantHolding] = None
    top_closed_position: Optional[SignificantHolding] = None
    top_increased_position: Optional[HoldingChange] = None
    top_decreased_position: Optional[HoldingChange] = None
    top_new_other_securities: Optional[SignificantHolding] = None
    top_closed_other_securities: Optional[SignificantHolding] = None
    top_increased_other_securities: Optional[HoldingChange] = None
    top_decreased_other_securities: Optional[HoldingChange] = None


class LatestStoriesResponse(BaseModel):
    """The response for the latest stories list endpoint."""

    stories: List[StorySummary]
    has_next_page: bool = False


FILING_QUERY_V2 = """
WITH RankedFilings AS (
    SELECT
        f.filing_id,
        f.company_id,
        f.accession_number,
        f.period_of_report,
        f.filing_date,
        f.created_at,
        ROW_NUMBER() OVER(PARTITION BY f.company_id ORDER BY f.filing_date DESC, f.created_at DESC) as rn
    FROM
        filings f
    WHERE
        f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
),
LatestFilings AS (
    SELECT
        filing_id,
        company_id,
        accession_number,
        period_of_report,
        filing_date,
        created_at
    FROM RankedFilings
    WHERE rn = 1
    ORDER BY filing_date DESC, created_at DESC
    LIMIT %(limit)s OFFSET %(offset)s
)
SELECT
    lf.filing_id,
    lf.company_id,
    lf.accession_number,
    lf.period_of_report,
    lf.filing_date,
    lf.created_at,
    c.company_name,
    c.cik_number,
    c.aum,
    pf.filing_id AS previous_filing_id,
    pf.accession_number AS previous_accession_number
FROM LatestFilings lf
JOIN companies c ON lf.company_id = c.company_id
LEFT JOIN LATERAL (
    SELECT filing_id, accession_number
    FROM filings
    WHERE company_id = lf.company_id
        AND period_of_report < lf.period_of_report
        AND form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ORDER BY period_of_report DESC, filing_date DESC
    LIMIT 1
) pf ON true
ORDER BY lf.filing_date DESC, lf.created_at DESC;
"""

FILTER_CANDIDATES_QUERY_V2 = """
    SELECT DISTINCT h.filing_id
    FROM holdings h
    JOIN title_of_class_table tc ON h.title_of_class = tc.id
    WHERE
        h.filing_id = ANY(%(candidate_filing_ids)s)
      AND tc.is_common_stock = TRUE;
"""

MODIFIED_OPTIMISED_STORIES_QUERY = """
    WITH FilingsWithPrevious AS (
        SELECT * FROM (VALUES %s) AS t (
            filing_id, company_id, accession_number, period_of_report, filing_date,
            created_at,
            company_name, cik_number, aum, previous_filing_id, previous_accession_number
        )
        WHERE filing_id = ANY(%(valid_filing_ids)s)
    ),
    AggregatedHoldings AS (
        SELECT
            h.filing_id,
            i.cusip,
            MIN(i.issuer_name) as issuer_name,
            SUM(h.value) as total_value,
            SUM(h.shares_or_principal_amount) as total_shares,
            tc.is_common_stock
        FROM holdings h
        JOIN issuers i ON h.issuer_id = i.issuer_id
        JOIN title_of_class_table tc ON h.title_of_class = tc.id
        LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
        WHERE h.filing_id = ANY(%(filing_ids_to_process)s)
          AND (poc.name IS NULL OR upper(poc.name) NOT IN ('PUT', 'CALL'))
        GROUP BY h.filing_id, i.cusip, tc.is_common_stock
    ),
    HoldingsComparison AS (
        SELECT
            fwp.filing_id,
            hc.*
        FROM
            FilingsWithPrevious fwp
        LEFT JOIN LATERAL (
            SELECT
                COALESCE(curr.cusip, prev.cusip) AS cusip,
                COALESCE(curr.issuer_name, prev.issuer_name) AS issuer_name,
                CASE
                    WHEN curr.total_shares > 0 AND COALESCE(curr.is_common_stock, prev.is_common_stock) AND (curr.total_value::numeric / curr.total_shares) < 1.0
                    THEN curr.total_value * 1000
                    ELSE curr.total_value
                END AS current_value,
                curr.total_shares AS current_shares,
                CASE
                    WHEN prev.total_shares > 0 AND COALESCE(curr.is_common_stock, prev.is_common_stock) AND (prev.total_value::numeric / prev.total_shares) < 1.0
                    THEN prev.total_value * 1000
                    ELSE prev.total_value
                END AS previous_value,
                prev.total_shares AS previous_shares,
                (curr.total_shares - prev.total_shares) AS change_in_share,
                CASE
                    WHEN prev.total_shares > 0 THEN ((curr.total_shares - prev.total_shares)::numeric / prev.total_shares) * 100
                    ELSE NULL
                END AS percent_change,
                CASE
                    WHEN prev.cusip IS NULL THEN 'new'
                    WHEN curr.cusip IS NULL THEN 'closed'
                    WHEN curr.total_shares > prev.total_shares THEN 'increased'
                    WHEN curr.total_shares < prev.total_shares THEN 'decreased'
                    ELSE 'unchanged'
                END as change_type,
                COALESCE(curr.is_common_stock, prev.is_common_stock) AS is_common_stock
            FROM
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.filing_id) AS curr
            FULL OUTER JOIN
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.previous_filing_id) AS prev
            ON curr.cusip = prev.cusip AND curr.is_common_stock = prev.is_common_stock
            WHERE
                (curr.cusip IS NOT NULL OR prev.cusip IS NOT NULL)
        ) hc ON true -- hc = holdings comparison
        WHERE fwp.previous_filing_id IS NOT NULL
    ),
    RankedChanges AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY filing_id, is_common_stock, change_type
                ORDER BY
                    -- For new/closed positions, rank by the position's value.
                    CASE WHEN change_type IN ('new', 'closed') THEN COALESCE(current_value, previous_value) END DESC,
                    -- For increased/decreased, rank by the absolute percentage change.
                    CASE WHEN change_type IN ('increased', 'decreased') THEN ABS(percent_change) END DESC
            ) as rn
        FROM HoldingsComparison
        WHERE change_type IN ('new', 'closed', 'increased', 'decreased')
    )
    SELECT
        fwp.cik_number AS cik,
        fwp.aum,
        fwp.accession_number AS latest_accession_number,
        fwp.previous_accession_number,
        fwp.company_name,
        fwp.period_of_report AS reporting_period,
        fwp.filing_date,
        -- Common Stock - New
        MAX(CASE WHEN rc.change_type = 'new' AND rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_new_issuer,
        MAX(CASE WHEN rc.change_type = 'new' AND rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_new_cusip,
        MAX(CASE WHEN rc.change_type = 'new' AND rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_new_shares,
        MAX(CASE WHEN rc.change_type = 'new' AND rc.is_common_stock AND rc.rn = 1 THEN rc.current_value END) AS top_new_value,
        MAX(CASE WHEN rc.change_type = 'new' AND rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            CASE
                WHEN (rc.current_value::numeric / rc.current_shares) < 1.0 THEN (rc.current_value::numeric * 1000) / rc.current_shares
                ELSE (rc.current_value::numeric) / rc.current_shares
            END
        ELSE 0 END) AS top_new_price,
        -- Common Stock - Closed
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_closed_issuer,
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_closed_cusip,
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 THEN rc.previous_shares END) AS top_closed_shares,
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 THEN rc.previous_value END) AS top_closed_value,
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 AND rc.previous_shares > 0 THEN
            CASE
                WHEN (rc.previous_value::numeric / rc.previous_shares) < 1.0 THEN (rc.previous_value::numeric * 1000) / rc.previous_shares
                ELSE (rc.previous_value::numeric) / rc.previous_shares
            END
        ELSE 0 END) AS top_closed_price,
        -- Common Stock - Increased
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_increased_issuer,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_increased_cusip,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_increased_shares,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.change_in_share END) AS top_increased_change_in_share,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.percent_change END) AS top_increased_percent_change,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            CASE
                WHEN (rc.current_value::numeric / rc.current_shares) < 1.0 THEN (rc.current_value::numeric * 1000) / rc.current_shares
                ELSE (rc.current_value::numeric) / rc.current_shares
            END
        ELSE 0 END) AS top_increased_price,
        -- Common Stock - Decreased
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_decreased_issuer,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_decreased_cusip,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_decreased_shares,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.change_in_share END) AS top_decreased_change_in_share,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.percent_change END) AS top_decreased_percent_change,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            CASE
                WHEN (rc.current_value::numeric / rc.current_shares) < 1.0 THEN (rc.current_value::numeric * 1000) / rc.current_shares
                ELSE (rc.current_value::numeric) / rc.current_shares
            END
        ELSE 0 END) AS top_decreased_price,
        -- Other Securities - New
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_new_other_issuer,
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_new_other_cusip,
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_new_other_shares,
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.current_value END) AS top_new_other_value,
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            CASE
                WHEN (rc.current_value::numeric / rc.current_shares) < 1.0 THEN (rc.current_value::numeric * 1000) / rc.current_shares
                ELSE (rc.current_value::numeric) / rc.current_shares
            END
        ELSE 0 END) AS top_new_other_price,  
        -- Other Securities - Closed
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_closed_other_issuer,
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_closed_other_cusip,
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.previous_shares END) AS top_closed_other_shares,
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.previous_value END) AS top_closed_other_value,
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 AND rc.previous_shares > 0 THEN
            CASE
                WHEN (rc.previous_value::numeric / rc.previous_shares) < 1.0 THEN (rc.previous_value::numeric * 1000) / rc.previous_shares
                ELSE (rc.previous_value::numeric) / rc.previous_shares
            END
        ELSE 0 END) AS top_closed_other_price,
        -- Other Securities - Increased
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_increased_other_issuer,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_increased_other_cusip,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_increased_other_shares,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.change_in_share END) AS top_increased_other_change_in_share,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.percent_change END) AS top_increased_other_percent_change,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            CASE
                WHEN (rc.current_value::numeric / rc.current_shares) < 1.0 THEN (rc.current_value::numeric * 1000) / rc.current_shares
                ELSE (rc.current_value::numeric) / rc.current_shares
            END
        ELSE 0 END) AS top_increased_other_price,
        -- Other Securities - Decreased
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_decreased_other_issuer,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_decreased_other_cusip,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_decreased_other_shares,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.change_in_share END) AS top_decreased_other_change_in_share,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.percent_change END) AS top_decreased_other_percent_change,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            CASE
                WHEN (rc.current_value::numeric / rc.current_shares) < 1.0 THEN (rc.current_value::numeric * 1000) / rc.current_shares
                ELSE (rc.current_value::numeric) / rc.current_shares
            END
        ELSE 0 END) AS top_decreased_other_price
    FROM
        FilingsWithPrevious fwp
    LEFT JOIN
        RankedChanges rc ON fwp.filing_id = rc.filing_id
    GROUP BY
        fwp.filing_id, fwp.cik_number, fwp.aum, fwp.accession_number, fwp.previous_accession_number,
        fwp.company_name, fwp.period_of_report, fwp.filing_date, fwp.created_at
    ORDER BY
        fwp.filing_date DESC, fwp.created_at DESC;
"""


@app.get("/stories/latest/v2", response_model=LatestStoriesResponse)
@limiter.limit("20/minute")
def get_latest_stories_v2(
    request: Request,
    limit: int = Query(20, description="Number of stories to return", ge=1, le=50),
    offset: int = Query(
        0, description="Number of stories to skip for pagination", ge=0
    ),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieves a list of the latest filings, each with a summary of its most
    significant new holding.

    **Note:** This endpoint explicitly filters for candidate filings that contain
    at least one Common Stock holding. Funds exclusively trading ETFs, options,
    or other non-common securities are intentionally excluded from this feed.
    """
    try:
        # Get filings
        logger.info(
            f"Fetching latest stories v2 with limit {limit} and offset {offset}"
        )
        db.execute(FILING_QUERY_V2, {"limit": limit + 1, "offset": offset})
        candidate_filings = db.fetchall()

        has_next_page = len(candidate_filings) > limit
        candidates_to_process = candidate_filings[:limit]

        if not candidates_to_process:
            logger.info("No candidate filings found for the given limit and offset.")
            return LatestStoriesResponse(stories=[])

        candidate_filing_ids = [f["filing_id"] for f in candidates_to_process]
        params = {
            "candidate_filing_ids": candidate_filing_ids,
            "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
        }
        # Filter filings
        logger.info("Filtering candidate filings")
        db.execute(FILTER_CANDIDATES_QUERY_V2, params)
        valid_filing_ids = {row["filing_id"] for row in db.fetchall()}

        # Filter our candidate list in Python
        valid_filings = [
            f for f in candidate_filings if f["filing_id"] in valid_filing_ids
        ]

        if not valid_filings:
            logger.info("No valid filings found after filtering.")
            return LatestStoriesResponse(stories=[])

        filing_ids_to_process = set()
        filing_data_tuples = []

        for f in valid_filings:
            filing_ids_to_process.add(f["filing_id"])
            if f["previous_filing_id"]:
                filing_ids_to_process.add(f["previous_filing_id"])

            # Prepare data for the VALUES clause
            filing_data_tuples.append(
                (
                    f["filing_id"],
                    f["company_id"],
                    f["accession_number"],
                    f["period_of_report"],
                    f["filing_date"],
                    f["created_at"],
                    f["company_name"],
                    f["cik_number"],
                    f["aum"],
                    f["previous_filing_id"],
                    f["previous_accession_number"],
                )
            )

        values_string_list = []
        for t in filing_data_tuples:
            # db.mogrify() safely formats a single tuple
            values_string_list.append(
                db.mogrify("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", t).decode(
                    "utf-8"
                )
            )

        values_string = ",\n".join(values_string_list)

        # 2. Modify the query template to *remove* the %s and insert our safe string.
        # We use .format() here because we have already safely escaped the data.
        final_query_template = MODIFIED_OPTIMISED_STORIES_QUERY.replace(
            "%s", values_string
        )

        # 3. Create the dictionary for the *other* (named) parameters.
        final_query_params = {
            "valid_filing_ids": list(valid_filing_ids),
            "filing_ids_to_process": list(filing_ids_to_process),
            "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
        }
        db.execute(final_query_template, final_query_params)
        logger.debug("Executing final optimized stories query")

        results = db.fetchall()

        story_summaries = []
        for row in results:
            # Common Stock Holdings
            top_new = (
                SignificantHolding(
                    issuer_name=row["top_new_issuer"],
                    cusip=row["top_new_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_new_cusip"]),
                    shares_or_principal_amount=row["top_new_shares"],
                    value=row["top_new_value"],
                    price_per_share=row["top_new_price"],
                    change_type="new",
                )
                if row["top_new_issuer"]
                else None
            )

            top_closed = (
                SignificantHolding(
                    issuer_name=row["top_closed_issuer"],
                    cusip=row["top_closed_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_closed_cusip"]),
                    shares_or_principal_amount=row["top_closed_shares"],
                    value=row["top_closed_value"],
                    price_per_share=row["top_closed_price"],
                    change_type="closed",
                )
                if row["top_closed_issuer"]
                else None
            )

            top_increased = (
                HoldingChange(
                    issuer_name=row["top_increased_issuer"],
                    cusip=row["top_increased_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_increased_cusip"]),
                    shares_or_principal_amount=row["top_increased_shares"],
                    change_in_share=row["top_increased_change_in_share"],
                    percent_change=row["top_increased_percent_change"],
                    price_per_share=row["top_increased_price"],
                    change_type="increased",
                )
                if row["top_increased_issuer"]
                else None
            )

            top_decreased = (
                HoldingChange(
                    issuer_name=row["top_decreased_issuer"],
                    cusip=row["top_decreased_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_decreased_cusip"]),
                    shares_or_principal_amount=row["top_decreased_shares"],
                    change_in_share=row["top_decreased_change_in_share"],
                    percent_change=row["top_decreased_percent_change"],
                    price_per_share=row["top_decreased_price"],
                    change_type="decreased",
                )
                if row["top_decreased_issuer"]
                else None
            )

            # Other Securities Holdings (assuming similar aliases like 'top_new_other_issuer')
            top_new_other = (
                SignificantHolding(
                    issuer_name=row["top_new_other_issuer"],
                    cusip=row["top_new_other_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_new_other_cusip"]),
                    shares_or_principal_amount=row["top_new_other_shares"],
                    value=row["top_new_other_value"],
                    price_per_share=row["top_new_other_price"],
                    change_type="new",
                )
                if row.get("top_new_other_issuer")
                else None
            )

            top_closed_other = (
                SignificantHolding(
                    issuer_name=row["top_closed_other_issuer"],
                    cusip=row["top_closed_other_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_closed_other_cusip"]),
                    shares_or_principal_amount=row["top_closed_other_shares"],
                    value=row["top_closed_other_value"],
                    price_per_share=row["top_closed_other_price"],
                    change_type="closed",
                )
                if row.get("top_closed_other_issuer")
                else None
            )

            top_increased_other = (
                HoldingChange(
                    issuer_name=row["top_increased_other_issuer"],
                    cusip=row["top_increased_other_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_increased_other_cusip"]),
                    shares_or_principal_amount=row["top_increased_other_shares"],
                    change_in_share=row["top_increased_other_change_in_share"],
                    percent_change=row["top_increased_other_percent_change"],
                    price_per_unit=row["top_increased_other_price"],
                    change_type="increased",
                )
                if row.get("top_increased_other_issuer")
                else None
            )

            top_decreased_other = (
                HoldingChange(
                    issuer_name=row["top_decreased_other_issuer"],
                    cusip=row["top_decreased_other_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_decreased_other_cusip"]),
                    shares_or_principal_amount=row["top_decreased_other_shares"],
                    change_in_share=row["top_decreased_other_change_in_share"],
                    percent_change=row["top_decreased_other_percent_change"],
                    price_per_unit=row["top_decreased_other_price"],
                    change_type="decreased",
                )
                if row.get("top_decreased_other_issuer")
                else None
            )

            story_summaries.append(
                StorySummary(
                    cik=row["cik"],
                    aum=row["aum"],
                    latest_accession_number=row["latest_accession_number"],
                    previous_accession_number=row["previous_accession_number"],
                    company_name=row["company_name"],
                    reporting_period=row["reporting_period"],
                    filing_date=row["filing_date"],
                    # Common Stock
                    top_new_position=top_new,
                    top_closed_position=top_closed,
                    top_increased_position=top_increased,
                    top_decreased_position=top_decreased,
                    # Other Securities
                    top_new_other_securities=top_new_other,
                    top_closed_other_securities=top_closed_other,
                    top_increased_other_securities=top_increased_other,
                    top_decreased_other_securities=top_decreased_other,
                )
            )

        return LatestStoriesResponse(
            stories=story_summaries, has_next_page=has_next_page
        )

    except Exception as e:
        logger.error(f"Error fetching latest stories v2: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=INTERNAL_ERROR_DETAIL
        )


LATEST_ACTIVITY_QUERY_V3 = """
    WITH FilingsWithPrevious AS (
        SELECT * FROM (VALUES %s) AS t (
            filing_id, company_id, accession_number, period_of_report, filing_date,
            created_at,
            company_name, cik_number, aum, previous_filing_id, previous_accession_number
        )
        WHERE filing_id = ANY(%(valid_filing_ids)s)
    ),
    AggregatedHoldings AS (
        SELECT
            h.filing_id,
            i.cusip,
            MIN(i.issuer_name) as issuer_name,
            SUM(h.value) as total_value,
            SUM(h.shares_or_principal_amount) as total_shares,
            tc.is_common_stock
        FROM holdings h
        JOIN issuers i ON h.issuer_id = i.issuer_id
        JOIN title_of_class_table tc ON h.title_of_class = tc.id
        LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
        WHERE h.filing_id = ANY(%(filing_ids_to_process)s)
          AND (poc.name IS NULL OR upper(poc.name) NOT IN ('PUT', 'CALL'))
        GROUP BY h.filing_id, i.cusip, tc.is_common_stock
    ),
    HoldingsComparison AS (
        SELECT
            fwp.filing_id,
            fwp.aum,
            fwp.cik_number,
            fwp.company_name,
            fwp.accession_number,
            fwp.previous_accession_number,
            fwp.period_of_report,
            fwp.filing_date,
            fwp.created_at,
            hc.cusip,
            hc.issuer_name,
            hc.current_value,
            hc.current_shares,
            hc.previous_value,
            hc.previous_shares,
            hc.change_in_share,
            hc.percent_change,
            hc.change_type,
            hc.is_common_stock
        FROM
            FilingsWithPrevious fwp
        LEFT JOIN LATERAL (
            SELECT
                COALESCE(curr.cusip, prev.cusip) AS cusip,
                COALESCE(curr.issuer_name, prev.issuer_name) AS issuer_name,
                curr.total_value AS current_value,
                curr.total_shares AS current_shares,
                prev.total_value AS previous_value,
                prev.total_shares AS previous_shares,
                (curr.total_shares - prev.total_shares) AS change_in_share,
                CASE
                    WHEN prev.total_shares > 0 THEN ((curr.total_shares - prev.total_shares)::numeric / prev.total_shares) * 100
                    ELSE NULL
                END AS percent_change,
                CASE
                    WHEN prev.cusip IS NULL THEN 'new'
                    WHEN curr.cusip IS NULL THEN 'closed'
                    WHEN curr.total_shares > prev.total_shares THEN 'increased'
                    WHEN curr.total_shares < prev.total_shares THEN 'decreased'
                    ELSE 'unchanged'
                END as change_type,
                COALESCE(curr.is_common_stock, prev.is_common_stock) AS is_common_stock
            FROM
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.filing_id) AS curr
            FULL OUTER JOIN
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.previous_filing_id) AS prev
                ON curr.cusip = prev.cusip AND curr.is_common_stock = prev.is_common_stock
            WHERE (curr.cusip IS NOT NULL OR prev.cusip IS NOT NULL)
        ) hc ON true
        WHERE
            fwp.previous_filing_id IS NOT NULL
    )
    SELECT
        hc.cik_number AS cik,
        hc.company_name,
        hc.accession_number AS latest_accession_number,
        hc.previous_accession_number,
        hc.period_of_report AS reporting_period,
        hc.filing_date,
        hc.issuer_name,
        hc.cusip,
        hc.is_common_stock,
        hc.change_type,
        hc.current_shares,
        hc.previous_shares,
        hc.change_in_share,
        ROUND(hc.percent_change::numeric, 2) AS percent_change,
        hc.current_value,
        hc.previous_value,
        ABS(COALESCE(hc.current_value, 0) - COALESCE(hc.previous_value, 0)) AS absolute_value_change,
        hc.aum,
        ROUND(
            CASE
                WHEN hc.current_shares > 0 THEN
                    CASE
                        -- Only apply 1000x logic IF it is common stock AND price is < 1.0
                        WHEN hc.is_common_stock AND (hc.current_value::numeric / hc.current_shares) < 1.0 
                        THEN (hc.current_value::numeric * 1000) / hc.current_shares
                        
                        -- Otherwise (not common stock OR price is >= 1.0), calculate normally
                        ELSE (hc.current_value::numeric) / hc.current_shares
                    END
                ELSE 0
            END, 2
        ) AS current_price_per_share,
        ROUND(
            CASE
                WHEN hc.previous_shares > 0 THEN
                    CASE
                        -- Only apply 1000x logic IF it is common stock AND price is < 1.0
                        WHEN hc.is_common_stock AND (hc.previous_value::numeric / hc.previous_shares) < 1.0 
                        THEN (hc.previous_value::numeric * 1000) / hc.previous_shares
                        
                        -- Otherwise (not common stock OR price is >= 1.0), calculate normally
                        ELSE (hc.previous_value::numeric) / hc.previous_shares
                    END
                ELSE 0
            END, 2
        ) AS previous_price_per_share
    FROM
        HoldingsComparison hc
    WHERE
        hc.change_type IN ('new', 'closed', 'increased', 'decreased')
    ORDER BY
        hc.filing_date DESC, hc.created_at DESC;
"""


@app.get("/activity/latest/v3", response_model=LatestActivityResponse)
def get_latest_activity_v3(
    request: Request,
    limit: int = Query(
        3, description="Number of *companies* to fetch stories for", ge=1, le=50
    ),
    offset: int = Query(
        0, description="Number of *companies* to skip for pagination", ge=0
    ),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieves a flat list of all significant holding changes (headlines)
    from the latest batch of company filings.

    **Note:** This endpoint explicitly filters for candidate filings that contain
    at least one Common Stock holding. Funds exclusively trading ETFs, options,
    or other non-common securities are intentionally excluded from this feed.
    """
    try:
        # Step 1: Get candidate companies (paginated)
        db.execute(FILING_QUERY_V2, {"limit": limit + 1, "offset": offset})
        candidate_filings = db.fetchall()

        has_next_page = len(candidate_filings) > limit
        candidates_to_process = candidate_filings[:limit]

        if not candidates_to_process:
            return LatestActivityResponse(activities=[])

        # Step 2: Filter candidates to only those with common stock
        candidate_filing_ids = [f["filing_id"] for f in candidates_to_process]
        params = {
            "candidate_filing_ids": candidate_filing_ids,
            "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
        }
        db.execute(FILTER_CANDIDATES_QUERY_V2, params)
        valid_filing_ids = {row["filing_id"] for row in db.fetchall()}

        valid_filings = [
            f for f in candidates_to_process if f["filing_id"] in valid_filing_ids
        ]

        if not valid_filings:
            return LatestActivityResponse(activities=[])

        # Step 3: Prepare the VALUES list and parameters for the main query
        filing_ids_to_process = set()
        filing_data_tuples = []

        for f in valid_filings:
            filing_ids_to_process.add(f["filing_id"])
            if f["previous_filing_id"]:
                filing_ids_to_process.add(f["previous_filing_id"])

            filing_data_tuples.append(
                (
                    f["filing_id"],
                    f["company_id"],
                    f["accession_number"],
                    f["period_of_report"],
                    f["filing_date"],
                    f["created_at"],
                    f["company_name"],
                    f["cik_number"],
                    f["aum"],
                    f["previous_filing_id"],
                    f["previous_accession_number"],
                )
            )

        # Safely create the VALUES string
        values_string_list = []
        for t in filing_data_tuples:
            values_string_list.append(
                db.mogrify("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", t).decode(
                    "utf-8"
                )
            )
        values_string = ",\n".join(values_string_list)

        # Prepare the final query
        final_query_template = LATEST_ACTIVITY_QUERY_V3.replace("%s", values_string)
        final_query_params = {
            "valid_filing_ids": list(valid_filing_ids),
            "filing_ids_to_process": list(filing_ids_to_process),
            "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
        }

        # Step 4: Execute the query and serialize the results
        db.execute(final_query_template, final_query_params)
        results = db.fetchall()

        # Serialize into our new HoldingActivity model and inject the ticker
        activities = []
        for row in results:
            activity_dict = dict(row)
            # Try to find the ticker based on the CUSIP
            activity_dict["ticker"] = CUSIP_TO_TICKER.get(activity_dict["cusip"])
            activities.append(HoldingActivity(**activity_dict))

        return LatestActivityResponse(
            activities=activities, has_next_page=has_next_page
        )

    except Exception as e:
        logger.error("Failed to fetch latest activity v3", exc_info=True)
        raise HTTPException(
            status_code=500, detail=INTERNAL_ERROR_DETAIL
        )


FILTER_CANDIDATES_QUERY_OPTIONS = """
    SELECT DISTINCT h.filing_id
    FROM holdings h
    JOIN put_or_call_table p ON h.put_or_call = p.id
    WHERE h.filing_id = ANY(%(candidate_filing_ids)s)
      AND p.name ILIKE ANY(ARRAY['Put', 'Call', 'PUT', 'CALL']);
"""

LATEST_ACTIVITY_QUERY_OPTIONS = """
    WITH FilingsWithPrevious AS (
        SELECT * FROM (VALUES %s) AS t (
            filing_id, company_id, accession_number, period_of_report, filing_date,
            created_at,
            company_name, cik_number, aum, previous_filing_id, previous_accession_number
        )
        WHERE filing_id = ANY(%(valid_filing_ids)s)
    ),
    AggregatedHoldings AS (
        SELECT
            h.filing_id,
            i.cusip,
            p.name AS put_or_call,
            MIN(i.issuer_name) as issuer_name,
            SUM(h.value) as total_value,
            SUM(h.shares_or_principal_amount) as total_shares
        FROM holdings h
        JOIN issuers i ON h.issuer_id = i.issuer_id
        JOIN put_or_call_table p ON h.put_or_call = p.id
        WHERE h.filing_id = ANY(%(filing_ids_to_process)s)
          AND p.name ILIKE ANY(ARRAY['Put', 'Call', 'PUT', 'CALL'])
        GROUP BY h.filing_id, i.cusip, p.name
    ),
    HoldingsComparison AS (
        SELECT
            fwp.filing_id,
            fwp.aum,
            fwp.cik_number,
            fwp.company_name,
            fwp.accession_number,
            fwp.previous_accession_number,
            fwp.period_of_report,
            fwp.filing_date,
            fwp.created_at,
            hc.cusip,
            hc.put_or_call,
            hc.issuer_name,
            hc.current_value,
            hc.current_shares,
            hc.previous_value,
            hc.previous_shares,
            hc.change_in_share,
            hc.percent_change,
            hc.change_type
        FROM
            FilingsWithPrevious fwp
        LEFT JOIN LATERAL (
            SELECT
                COALESCE(curr.cusip, prev.cusip) AS cusip,
                COALESCE(curr.put_or_call, prev.put_or_call) AS put_or_call,
                COALESCE(curr.issuer_name, prev.issuer_name) AS issuer_name,
                curr.total_value AS current_value,
                curr.total_shares AS current_shares,
                prev.total_value AS previous_value,
                prev.total_shares AS previous_shares,
                (curr.total_shares - prev.total_shares) AS change_in_share,
                CASE
                    WHEN prev.total_shares > 0 THEN ((curr.total_shares - prev.total_shares)::numeric / prev.total_shares) * 100
                    ELSE NULL
                END AS percent_change,
                CASE
                    WHEN prev.cusip IS NULL THEN 'new'
                    WHEN curr.cusip IS NULL THEN 'closed'
                    WHEN curr.total_shares > prev.total_shares THEN 'increased'
                    WHEN curr.total_shares < prev.total_shares THEN 'decreased'
                    ELSE 'unchanged'
                END as change_type
            FROM
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.filing_id) AS curr
            FULL OUTER JOIN
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.previous_filing_id) AS prev
                -- We must join on BOTH cusip and put_or_call to avoid overlapping Puts/Calls
                ON curr.cusip = prev.cusip AND curr.put_or_call = prev.put_or_call
            WHERE (curr.cusip IS NOT NULL OR prev.cusip IS NOT NULL)
        ) hc ON true
        WHERE
            fwp.previous_filing_id IS NOT NULL
    )
    SELECT
        hc.cik_number AS cik,
        hc.company_name,
        hc.accession_number AS latest_accession_number,
        hc.previous_accession_number,
        hc.period_of_report AS reporting_period,
        hc.filing_date,
        hc.issuer_name,
        hc.cusip,
        hc.put_or_call,
        false AS is_common_stock,
        hc.change_type,
        hc.current_shares,
        hc.previous_shares,
        hc.change_in_share,
        ROUND(hc.percent_change::numeric, 2) AS percent_change,
        hc.current_value,
        hc.previous_value,
        ABS(COALESCE(hc.current_value, 0) - COALESCE(hc.previous_value, 0)) AS absolute_value_change,
        hc.aum,
        ROUND(CASE WHEN hc.current_shares > 0 THEN (hc.current_value::numeric) / hc.current_shares ELSE 0 END, 2) AS current_price_per_share,
        ROUND(CASE WHEN hc.previous_shares > 0 THEN (hc.previous_value::numeric) / hc.previous_shares ELSE 0 END, 2) AS previous_price_per_share
    FROM
        HoldingsComparison hc
    WHERE
        hc.change_type IN ('new', 'closed', 'increased', 'decreased', 'unchanged')
    ORDER BY
        hc.filing_date DESC, hc.created_at DESC;
"""


@app.get("/activity/latest/options", response_model=LatestActivityResponse)
def get_latest_options_activity(
    request: Request,
    limit: int = Query(
        3, description="Number of *companies* to fetch options stories for", ge=1, le=50
    ),
    offset: int = Query(
        0, description="Number of *companies* to skip for pagination", ge=0
    ),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieves a flat list of all significant options (Put/Call) holding changes
    from the latest batch of company filings.
    """
    try:
        # Step 1: Get candidate companies (paginated)
        db.execute(FILING_QUERY_V2, {"limit": limit + 1, "offset": offset})
        candidate_filings = db.fetchall()

        has_next_page = len(candidate_filings) > limit
        candidates_to_process = candidate_filings[:limit]

        if not candidates_to_process:
            return LatestActivityResponse(activities=[])

        # Step 2: Filter candidates to only those holding Options (Put/Call)
        candidate_filing_ids = [f["filing_id"] for f in candidates_to_process]
        params = {
            "candidate_filing_ids": candidate_filing_ids,
        }
        db.execute(FILTER_CANDIDATES_QUERY_OPTIONS, params)
        valid_filing_ids = {row["filing_id"] for row in db.fetchall()}

        valid_filings = [
            f for f in candidates_to_process if f["filing_id"] in valid_filing_ids
        ]

        if not valid_filings:
            return LatestActivityResponse(activities=[])

        # Step 3: Prepare the VALUES list and parameters for the main query
        filing_ids_to_process = set()
        filing_data_tuples = []

        for f in valid_filings:
            filing_ids_to_process.add(f["filing_id"])
            if f["previous_filing_id"]:
                filing_ids_to_process.add(f["previous_filing_id"])

            filing_data_tuples.append(
                (
                    f["filing_id"],
                    f["company_id"],
                    f["accession_number"],
                    f["period_of_report"],
                    f["filing_date"],
                    f["created_at"],
                    f["company_name"],
                    f["cik_number"],
                    f["aum"],
                    f["previous_filing_id"],
                    f["previous_accession_number"],
                )
            )

        # Safely create the VALUES string
        values_string_list = []
        for t in filing_data_tuples:
            values_string_list.append(
                db.mogrify("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", t).decode(
                    "utf-8"
                )
            )
        values_string = ",\n".join(values_string_list)

        # Prepare the final query
        final_query_template = LATEST_ACTIVITY_QUERY_OPTIONS.replace(
            "%s", values_string
        )
        final_query_params = {
            "valid_filing_ids": list(valid_filing_ids),
            "filing_ids_to_process": list(filing_ids_to_process),
        }

        # Step 4: Execute the query and serialize the results
        db.execute(final_query_template, final_query_params)
        results = db.fetchall()

        # Serialize into our HoldingActivity model and inject the ticker
        activities = []
        for row in results:
            activity_dict = dict(row)
            activity_dict["ticker"] = CUSIP_TO_TICKER.get(activity_dict["cusip"])
            activities.append(HoldingActivity(**activity_dict))

        return LatestActivityResponse(
            activities=activities, has_next_page=has_next_page
        )

    except Exception as e:
        logger.error("Failed to fetch latest options activity", exc_info=True)
        raise HTTPException(
            status_code=500, detail=INTERNAL_ERROR_DETAIL
        )


@app.get("/api/search/companies", response_model=List[Dict[str, str]])
def search_companies(
    request: Request,
    q: str = Query(
        ..., min_length=2, description="Search term for company name or CIK"
    ),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Search for companies by name or CIK for autocomplete.
    """
    if len(q) < 2:
        return []

    search_name = f"%{q}%"
    search_cik = f"{q}%"  # CIKs are usually searched from the start

    query = """
        SELECT
            company_name,
            cik_number
        FROM companies
        WHERE
            company_name ILIKE %s
            OR cik_number::text ILIKE %s
        LIMIT 10;
    """
    try:
        db.execute(query, (search_name, search_cik))
        results = db.fetchall()

        companies = [
            {"name": row["company_name"], "cik": str(row["cik_number"])}
            for row in results
        ]
        return companies
    except Exception as e:
        logger.error(f"Error during company search: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error during company search")


def resolve_identifiers(
    identifier: str, db: psycopg2.extensions.cursor
) -> tuple[str | None, str | None]:
    """
    Takes an unknown identifier (Ticker or CUSIP) and returns both (ticker, cusip).
    Implements a multi-tier waterfall lookup to ensure both are found.
    """
    clean_id = identifier.strip().upper()
    ticker = None
    cusip = None

    is_likely_ticker = len(clean_id) <= 5 and clean_id.isalpha()

    # ---------------------------------------------------------
    # PATH A: Assume input is a TICKER
    # ---------------------------------------------------------
    if clean_id in TICKER_TO_CUSIP:
        ticker = clean_id
        # JSON returns a list, extract the first item
        cusip_list = TICKER_TO_CUSIP[ticker]
        if cusip_list and isinstance(cusip_list, list):
            cusip = cusip_list[0]
        elif isinstance(cusip_list, str):
            cusip = cusip_list

    elif clean_id in FREE_FLOAT_DATA:
        ticker = clean_id

    elif is_likely_ticker:
        ticker = clean_id

    # ---------------------------------------------------------
    # PATH B: Assume input is a CUSIP
    # ---------------------------------------------------------
    else:
        cusip = clean_id

        # 1. Try reverse lookup using our new global dict
        if cusip in CUSIP_TO_TICKER:
            ticker = CUSIP_TO_TICKER[cusip]

        # 2. If no ticker yet, try CUSIP -> CIK -> Ticker (JSON lists)
        if not ticker:
            cik_list = CUSIP_TO_CIK.get(cusip)
            if cik_list and isinstance(cik_list, list) and len(cik_list) > 0:
                cik_str = str(cik_list[0])  # Extract first CIK

                ticker_list = CIK_TO_TICKER.get(cik_str)
                if (
                    ticker_list
                    and isinstance(ticker_list, list)
                    and len(ticker_list) > 0
                ):
                    ticker = ticker_list[0].upper()  # Extract first Ticker

    # ---------------------------------------------------------
    # PATH C: DATABASE FALLBACKS
    # ---------------------------------------------------------
    try:
        if cusip and not ticker:
            db.execute("SELECT symbol FROM issuers WHERE cusip = %s LIMIT 1", (cusip,))
            db_res = db.fetchone()
            if db_res and db_res.get("symbol"):
                ticker = db_res["symbol"].strip().upper()

        elif ticker and not cusip:
            db.execute("SELECT cusip FROM issuers WHERE symbol = %s LIMIT 1", (ticker,))
            db_res = db.fetchone()
            if db_res and db_res.get("cusip"):
                cusip = db_res["cusip"].strip().upper()
    except Exception as e:
        logger.warning(f"Database identifier fallback failed for {clean_id}: {e}")

    return ticker, cusip


def get_free_float(ticker: str) -> float | None:
    """
    Retrieves free float in millions. Uses the local CSV cache first.
    If not found, fetches from Yahoo Finance and caches it.
    """
    if not ticker:
        return None

    ticker = ticker.upper()

    # 1. Check local cache (loaded from CSV previously)
    if ticker in FREE_FLOAT_DATA:
        return FREE_FLOAT_DATA[ticker]

    # 2. Fallback: Fetch dynamically
    try:
        logger.info(f"Ticker {ticker} not in CSV. Fetching float from Yahoo Finance...")
        stock = yf.Ticker(ticker)

        # Yahoo Finance returns float shares as a raw number (e.g., 50000000)
        float_shares_raw = stock.info.get("floatShares")

        if float_shares_raw:
            # Convert to millions to match your CSV format
            float_in_millions = float_shares_raw / 1_000_000.0

            # Cache it so we don't hit the API again for this ticker
            FREE_FLOAT_DATA[ticker] = float_in_millions
            logger.info(
                f"Successfully fetched and cached float for {ticker}: {float_in_millions}M"
            )
            return float_in_millions
        else:
            logger.warning(f"Yahoo Finance has no float data for {ticker}.")
            # Cache a None or 0 to prevent repeated failed API calls?
            # For now, we just return None.
            return None

    except Exception as e:
        logger.error(f"Error fetching float for {ticker} via yfinance: {e}")
        return None


@app.get("/api/v1/flow/daily/{identifier}", response_model=DailyFlowResponse)
def get_daily_stock_flow(
    request: Request,
    identifier: str,
    days: int = Query(1, description="Number of days to look back", ge=1, le=365),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Aggregates buying/selling flow based on the FILING DATE.
    This shows 'what was reported today' rather than 'what happened in Q3'.
    """
    ticker, clean_cusip = resolve_identifiers(identifier, db)

    if not clean_cusip:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve {identifier} to a valid CUSIP for analysis.",
        )

    logger.info(
        f"Daily Flow: Requested {identifier} -> Resolved Ticker: {ticker}, CUSIP: {clean_cusip}"
    )

    start_date = dt.date.today() - dt.timedelta(days=days)

    query = """
    WITH TargetIssuer AS (
        SELECT issuer_id FROM issuers WHERE cusip = %s LIMIT 1
    ),
    CompaniesWithStock AS (
        -- 1. Find ONLY the companies that have EVER held this specific stock
        SELECT DISTINCT f.company_id
        FROM holdings h
        JOIN filings f ON h.filing_id = f.filing_id
        WHERE h.issuer_id = (SELECT issuer_id FROM TargetIssuer)
    ),
    RelevantFilings AS (
        -- 2. Only look at filings in our date window FOR THOSE SPECIFIC COMPANIES
        SELECT f.filing_id, f.company_id, f.filing_date, f.period_of_report
        FROM filings f
        JOIN CompaniesWithStock cws ON f.company_id = cws.company_id
        WHERE f.filing_date >= %s
          AND f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ),
    PreviousFilings AS (
        -- 3. Now we only run this subquery a few dozen times instead of thousands
        SELECT
            rf.filing_id AS current_filing_id,
            (
                SELECT f2.filing_id
                FROM filings f2
                WHERE f2.company_id = rf.company_id
                  AND f2.period_of_report < rf.period_of_report
                  AND f2.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
                ORDER BY f2.period_of_report DESC, f2.filing_date DESC, f2.accession_number DESC
                LIMIT 1
            ) AS previous_filing_id
        FROM RelevantFilings rf
    ),
    StockHoldings AS (
        -- 4. Pre-fetch the exact share counts for this specific stock
        SELECT filing_id, shares_or_principal_amount
        FROM holdings
        WHERE issuer_id = (SELECT issuer_id FROM TargetIssuer)
    ),
    HoldingsComparison AS (
        -- 5. Join it all together instantly
        SELECT
            rf.filing_date,
            COALESCE(h_curr.shares_or_principal_amount, 0) as current_shares,
            COALESCE(h_prev.shares_or_principal_amount, 0) as previous_shares
        FROM RelevantFilings rf
        JOIN PreviousFilings pf ON rf.filing_id = pf.current_filing_id
        LEFT JOIN StockHoldings h_curr ON rf.filing_id = h_curr.filing_id
        LEFT JOIN StockHoldings h_prev ON pf.previous_filing_id = h_prev.filing_id
        WHERE h_curr.shares_or_principal_amount IS NOT NULL
           OR h_prev.shares_or_principal_amount IS NOT NULL
    )
    -- 6. Group by Filing Date
    SELECT
        filing_date,
        SUM(CASE WHEN current_shares > previous_shares THEN (current_shares - previous_shares) ELSE 0 END) as gross_buying,
        SUM(CASE WHEN current_shares < previous_shares THEN ABS(current_shares - previous_shares) ELSE 0 END) as gross_selling,
        SUM(current_shares - previous_shares) as net_change,
        SUM(previous_shares) as total_previous_shares
    FROM HoldingsComparison
    GROUP BY filing_date
    ORDER BY filing_date DESC;
    """

    try:
        # Pass params: start_date, then cusip twice (for current and prev join)
        db.execute(query, (clean_cusip, start_date))
        results = db.fetchall()

        daily_data = []
        for row in results:
            # Safely handle nulls if there were no previous shares
            prev_shares = (
                float(row["total_previous_shares"])
                if row["total_previous_shares"]
                else 0.0
            )
            net_change = float(row["net_change"])

            pct_change = None
            if prev_shares > 0:
                pct_change = (net_change / prev_shares) * 100

            daily_data.append(
                DailyFlowEntry(
                    date=row["filing_date"],
                    gross_buying=float(row["gross_buying"]),
                    gross_selling=float(row["gross_selling"]),
                    net_change=net_change,
                    percent_change=pct_change,
                    net_change_pct_float=None,  # Handled below
                )
            )

        free_float_shares = None

        if ticker:
            float_in_millions = get_free_float(ticker)
            if float_in_millions and float_in_millions > 0:
                free_float_shares = float_in_millions * 1_000_000
                for entry in daily_data:
                    entry.net_change_pct_float = (
                        entry.net_change / free_float_shares
                    ) * 100

        return DailyFlowResponse(
            ticker=ticker,
            cusip=clean_cusip,
            free_float_shares=free_float_shares,
            daily_data=daily_data,
        )

    except Exception as e:
        logger.error(
            f"Error fetching daily stock flow for identifier {identifier}: {str(e)}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@app.get("/api/v1/flow/aggregate/{identifier}", response_model=AggregateFlowResponse)
def get_aggregate_stock_flow(
    request: Request,
    identifier: str,
    days: int = Query(14, description="Number of days to look back", ge=1, le=365),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Aggregates total buying/selling flow over the last X days into a single summary.
    """
    ticker, clean_cusip = resolve_identifiers(identifier, db)

    if not clean_cusip:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve {identifier} to a valid CUSIP for analysis.",
        )

    logger.info(
        f"Daily Flow: Requested {identifier} -> Resolved Ticker: {ticker}, CUSIP: {clean_cusip}"
    )

    start_date = dt.date.today() - dt.timedelta(days=days)

    query = """
    WITH TargetIssuer AS (
        SELECT issuer_id FROM issuers WHERE cusip = %s LIMIT 1
    ),
    CompaniesWithStock AS (
        SELECT DISTINCT f.company_id
        FROM holdings h
        JOIN filings f ON h.filing_id = f.filing_id
        WHERE h.issuer_id = (SELECT issuer_id FROM TargetIssuer)
    ),
    RelevantFilings AS (
        SELECT f.filing_id, f.company_id, f.filing_date, f.period_of_report
        FROM filings f
        JOIN CompaniesWithStock cws ON f.company_id = cws.company_id
        WHERE f.filing_date >= %s
          AND f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ),
    PreviousFilings AS (
        SELECT
            rf.filing_id AS current_filing_id,
            (
                SELECT f2.filing_id
                FROM filings f2
                WHERE f2.company_id = rf.company_id
                  AND f2.period_of_report < rf.period_of_report
                  AND f2.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
                ORDER BY f2.period_of_report DESC, f2.filing_date DESC, f2.accession_number DESC
                LIMIT 1
            ) AS previous_filing_id
        FROM RelevantFilings rf
    ),
    StockHoldings AS (
        SELECT filing_id, shares_or_principal_amount
        FROM holdings
        WHERE issuer_id = (SELECT issuer_id FROM TargetIssuer)
    ),
    HoldingsComparison AS (
        SELECT
            rf.filing_date,
            COALESCE(h_curr.shares_or_principal_amount, 0) as current_shares,
            COALESCE(h_prev.shares_or_principal_amount, 0) as previous_shares
        FROM RelevantFilings rf
        JOIN PreviousFilings pf ON rf.filing_id = pf.current_filing_id
        LEFT JOIN StockHoldings h_curr ON rf.filing_id = h_curr.filing_id
        LEFT JOIN StockHoldings h_prev ON pf.previous_filing_id = h_prev.filing_id
        WHERE h_curr.shares_or_principal_amount IS NOT NULL
           OR h_prev.shares_or_principal_amount IS NOT NULL
    )
    -- 6. Aggregate everything together
    SELECT
        COALESCE(SUM(CASE WHEN current_shares > previous_shares THEN (current_shares - previous_shares) ELSE 0 END), 0) as total_gross_buying,
        COALESCE(SUM(CASE WHEN current_shares < previous_shares THEN ABS(current_shares - previous_shares) ELSE 0 END), 0) as total_gross_selling,
        COALESCE(SUM(current_shares - previous_shares), 0) as total_net_change,
        COALESCE(SUM(previous_shares), 0) as total_previous_shares
    FROM HoldingsComparison;
    """

    try:
        db.execute(query, (clean_cusip, start_date))
        result = db.fetchone()

        if not result:
            return AggregateFlowResponse(
                ticker=ticker,
                cusip=clean_cusip,
                days_looked_back=days,
                gross_buying=0.0,
                gross_selling=0.0,
                net_change=0.0,
                net_change_pct_float=None,
                percent_change=None,
                free_float_shares=None,
            )

        net_change_val = float(result["total_net_change"])
        total_previous = float(result["total_previous_shares"])
        net_change_pct = None
        percent_change = None
        free_float_shares = None

        # --- 3. FREE FLOAT CALCULATION (Using ticker) ---
        if ticker:
            float_in_millions = get_free_float(ticker)
            if float_in_millions and float_in_millions > 0:
                free_float_shares = float_in_millions * 1_000_000
                net_change_pct = (net_change_val / free_float_shares) * 100

        # --- 4. PERCENT INCREASE/DECREASE CALCULATION ---
        if total_previous > 0:
            percent_change = (net_change_val / total_previous) * 100

        return AggregateFlowResponse(
            ticker=ticker,
            cusip=clean_cusip,
            days_looked_back=days,
            gross_buying=float(result["total_gross_buying"]),
            gross_selling=float(result["total_gross_selling"]),
            net_change=net_change_val,
            net_change_pct_float=net_change_pct,
            percent_change=percent_change,
            free_float_shares=free_float_shares,
        )

    except Exception as e:
        logger.error(
            f"Error fetching aggregate stock flow for identifier {identifier}: {str(e)}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@app.get("/api/v1/search/companies_by_aum", response_model=list[dict])
def search_companies_by_aum(
    request: Request,
    min_aum: Optional[int] = Query(None, description="Minimum AUM"),
    max_aum: Optional[int] = Query(None, description="Maximum AUM"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Search for companies within a specific Assets Under Management (AUM) range.
    """
    # Start with a base query
    query = """
        SELECT
            cik_number,
            company_name,
            aum
        FROM companies
        WHERE 1=1
    """
    params = []

    # Dynamically build the WHERE clause based on provided parameters
    if min_aum is not None:
        query += " AND aum >= %s"
        params.append(min_aum)

    if max_aum is not None:
        query += " AND aum <= %s"
        params.append(max_aum)

    # Add sorting and pagination
    query += " ORDER BY aum DESC NULLS LAST LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    try:
        # Execute the query safely to prevent SQL injection
        db.execute(query, tuple(params))
        results = db.fetchall()

        # Format the response
        companies = [
            {
                "cik": str(row["cik_number"]),
                "company_name": row["company_name"],
                "aum": row["aum"],
            }
            for row in results
        ]
        return companies

    except Exception as e:
        logger.error(f"Error searching companies by AUM: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@app.get("/api/v1/search/filings_by_aum", response_model=dict)
def search_filings_by_aum(
    request: Request,
    min_aum: Optional[int] = Query(None, description="Minimum AUM filter"),
    max_aum: Optional[int] = Query(None, description="Maximum AUM filter"),
    ciks: Optional[List[str]] = Query(
        None, description="List of company CIKs to include"
    ),
    limit: int = Query(100, ge=1, le=1000, description="Number of results to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    sort_by: str = Query(
        "created_at",
        description="Sort column: 'filing_date' or 'created_at' or 'period_of_report'",
    ),
    sort_order: str = Query("desc", description="Sort order: 'asc' or 'desc'"),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Search and filter filings based on Company AUM and specific CIKs,
    with pagination and sorting capabilities.
    """

    # 1. Validate sorting parameters to prevent SQL injection
    allowed_sort_columns = {
        "filing_date": "f.filing_date",
        "created_at": "f.created_at",
        "period_of_report": "f.period_of_report",
    }

    if sort_by not in allowed_sort_columns:
        raise HTTPException(
            status_code=400,
            detail="Invalid sort_by column. Use 'filing_date', 'created_at', or 'period_of_report'.",
        )

    if sort_order.lower() not in ["asc", "desc"]:
        raise HTTPException(
            status_code=400, detail="Invalid sort_order. Use 'asc' or 'desc'."
        )

    sort_column = allowed_sort_columns[sort_by]
    sort_dir = sort_order.upper()

    # 2. Build the WHERE clause dynamically
    where_clauses = ["1=1"]
    params = {}

    if min_aum is not None:
        where_clauses.append("c.aum >= %(min_aum)s")
        params["min_aum"] = min_aum

    if max_aum is not None:
        where_clauses.append("c.aum <= %(max_aum)s")
        params["max_aum"] = max_aum

    if ciks:
        # .lstrip("0") strips all leading zeros.
        # The 'or "0"' ensures that if someone literally inputs "0000000000",
        # it falls back to a single "0" instead of an empty string.
        clean_ciks = [cik.strip().lstrip("0") or "0" for cik in ciks]

        where_clauses.append("c.cik_number = ANY(%(ciks)s)")
        params["ciks"] = clean_ciks

    where_sql = " AND ".join(where_clauses)

    try:
        # 3. Get total count for precise pagination metadata
        count_query = f"""
            SELECT COUNT(*) 
            FROM filings f
            JOIN companies c ON f.company_id = c.company_id
            WHERE {where_sql}
        """

        db.execute(count_query, params)
        total_count = db.fetchone()["count"]

        if total_count == 0:
            return {
                "filings": [],
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "total": 0,
                    "has_more": False,
                },
            }

        # 4. Fetch the paginated and sorted data
        filings_query = f"""
            SELECT
                f.accession_number, f.form_type, f.filing_date, f.period_of_report,
                f.file_number, f.filing_directory, f.created_at, f.updated_at,
                c.company_name, c.cik_number, c.aum
            FROM filings f
            JOIN companies c ON f.company_id = c.company_id
            WHERE {where_sql}
            ORDER BY {sort_column} {sort_dir}
            LIMIT %(limit)s OFFSET %(offset)s
        """

        # Add pagination variables to the execution parameters
        params["limit"] = limit
        params["offset"] = offset

        db.execute(filings_query, params)
        filings_data = db.fetchall()

        # Determine if there are more pages
        has_more = (offset + len(filings_data)) < total_count

        return {
            "filings": filings_data,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total_count,
                "has_more": has_more,
            },
        }

    except Exception as e:
        logger.error(f"Error searching filings by AUM: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@app.get("/api/v1/flow/top-changes", response_model=TopStockChangesResponse)
def get_top_market_changes_today(
    request: Request,
    date: Optional[dt.date] = Query(
        None, description="Target filing date (YYYY-MM-DD). Defaults to today."
    ),
    sort_by: str = Query(
        "value", description="Sort by 'value' (dollar change) or 'shares' (share count)"
    ),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieves the top 50 stocks with the largest absolute net change across
    all institutional 13F filings processed on a specific date.
    """
    target_date = date or dt.date.today()

    if sort_by not in ["value", "shares"]:
        raise HTTPException(
            status_code=400, detail="Invalid sort_by option. Use 'value' or 'shares'."
        )

    # Determine safe SQL ordering expression based on choice
    order_by_clause = (
        "ABS(SUM(hc.value_change))"
        if sort_by == "value"
        else "ABS(SUM(hc.share_change))"
    )

    query = f"""
    WITH RelevantFilings AS (
        -- 1. Grab all filings that hit the system on the specified date
        SELECT f.filing_id, f.company_id, f.filing_date, f.period_of_report
        FROM filings f
        WHERE f.filing_date = %(date)s
          AND f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ),
    PreviousFilings AS (
        -- 2. Find the immediate previous filing for each company
        SELECT
            rf.filing_id AS current_filing_id,
            (
                SELECT f2.filing_id
                FROM filings f2
                WHERE f2.company_id = rf.company_id
                  AND f2.period_of_report < rf.period_of_report
                  AND f2.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
                ORDER BY f2.period_of_report DESC, f2.filing_date DESC, f2.accession_number DESC
                LIMIT 1
            ) AS previous_filing_id
        FROM RelevantFilings rf
    ),
    HoldingsComparison AS (
        -- 3. Side-by-side position changes per company, filtering for Common Stock 
        -- and applying your 1000x correction multiplier for low-price entries
        SELECT
            hc.issuer_id,
            (hc.current_shares - hc.previous_shares) AS share_change,
            (hc.current_value - hc.previous_value) AS value_change,
            CASE WHEN hc.current_shares > hc.previous_shares THEN (hc.current_shares - hc.previous_shares) ELSE 0 END AS buying_shares,
            CASE WHEN hc.current_shares < hc.previous_shares THEN (hc.previous_shares - hc.current_shares) ELSE 0 END AS selling_shares
        FROM RelevantFilings rf
        JOIN PreviousFilings pf ON rf.filing_id = pf.current_filing_id
        LEFT JOIN LATERAL (
            SELECT
                COALESCE(curr.issuer_id, prev.issuer_id) AS issuer_id,
                COALESCE(curr.shares_or_principal_amount, 0) AS current_shares,
                COALESCE(prev.shares_or_principal_amount, 0) AS previous_shares,
                COALESCE(curr.value, 0) AS current_value,
                COALESCE(prev.value, 0) AS previous_value
            FROM (
                SELECT h.issuer_id, h.shares_or_principal_amount, 
                       CASE WHEN h.shares_or_principal_amount > 0 AND (h.value::numeric / h.shares_or_principal_amount) < 1.0 THEN h.value * 1000 ELSE h.value END as value 
                FROM holdings h
                JOIN title_of_class_table tc ON h.title_of_class = tc.id
                WHERE h.filing_id = pf.current_filing_id
                  AND tc.is_common_stock = TRUE
            ) curr
            FULL OUTER JOIN (
                SELECT h.issuer_id, h.shares_or_principal_amount, 
                       CASE WHEN h.shares_or_principal_amount > 0 AND (h.value::numeric / h.shares_or_principal_amount) < 1.0 THEN h.value * 1000 ELSE h.value END as value 
                FROM holdings h
                JOIN title_of_class_table tc ON h.title_of_class = tc.id
                WHERE h.filing_id = pf.previous_filing_id
                  AND tc.is_common_stock = TRUE
            ) prev ON curr.issuer_id = prev.issuer_id
        ) hc ON TRUE
    )
    -- 4. Roll up entries market-wide per stock issuer
    SELECT
        i.issuer_name,
        i.cusip,
        i.symbol AS ticker,
        SUM(hc.share_change) AS net_shares_change,
        SUM(hc.value_change) AS net_value_change,
        SUM(ABS(hc.value_change)) AS absolute_value_change,
        SUM(hc.buying_shares) AS gross_buying_shares,
        SUM(hc.selling_shares) AS gross_selling_shares
    FROM HoldingsComparison hc
    JOIN issuers i ON hc.issuer_id = i.issuer_id
    GROUP BY i.issuer_id, i.issuer_name, i.cusip, i.symbol
    ORDER BY {order_by_clause} DESC
    LIMIT 50;
    """

    try:
        db.execute(
            query,
            {"date": target_date, "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS},
        )
        results = db.fetchall()

        stocks_data = []
        for row in results:
            stocks_data.append(
                TopStockChangeEntry(
                    issuer_name=row["issuer_name"],
                    cusip=row["cusip"],
                    # Fall back onto your JSON cross-map memory dictionary if DB ticker is null
                    ticker=row["ticker"] or CUSIP_TO_TICKER.get(row["cusip"]),
                    net_shares_change=float(row["net_shares_change"]),
                    net_value_change=float(row["net_value_change"]),
                    absolute_value_change=float(row["absolute_value_change"]),
                    gross_buying_shares=float(row["gross_buying_shares"]),
                    gross_selling_shares=float(row["gross_selling_shares"]),
                )
            )

        return TopStockChangesResponse(
            date=target_date, sort_by=sort_by, stocks=stocks_data
        )

    except Exception as e:
        logger.error(f"Error compiling top market stock flows: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=INTERNAL_ERROR_DETAIL
        )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
