import os
import sys
import logging
import math
import json
from collections import OrderedDict
from typing import Optional, Dict
import pandas as pd
import polars as pl
from openai import AsyncOpenAI
from slowapi import Limiter
from slowapi.util import get_remote_address

# ---------------------------------------------------------------------------
# Runtime & Environment Configuration
# ---------------------------------------------------------------------------
APP_ENV = os.getenv("APP_ENV", "production").lower().strip()
DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes", "on")
ENABLE_DOCS = DEBUG or os.getenv("ENABLE_DOCS", "false").lower() in ("1", "true", "yes", "on")
EDGAR_IDENTITY = os.getenv("EDGAR_IDENTITY", "26b610663e50@company.co.uk")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or os.getenv("DEEPSEEK_API_KEY", None)
DEEPSEEK_API_KEY = OPENROUTER_API_KEY  # Backwards compatibility alias
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1")
_default_ai_model = "deepseek-chat" if "api.deepseek.com" in AI_BASE_URL else "deepseek/deepseek-v4.1-flash"
AI_MODEL = os.getenv("AI_MODEL", _default_ai_model)
RATE_LIMIT = os.getenv("RATE_LIMIT", "120/minute")

COMMON_STOCK_TITLE_OF_CLASS = "COM|CL A|COMMON STOCK|STOCK|COM SHS|CAP STK CL"
OPTION_PUT_CALL_NAMES = frozenset({"PUT", "CALL"})

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
numeric_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(
    level=numeric_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("secapi")

# ---------------------------------------------------------------------------
# AI Client (OpenRouter / OpenAI compatible)
# ---------------------------------------------------------------------------
client: Optional[AsyncOpenAI] = None
if OPENROUTER_API_KEY:
    client = AsyncOpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url=AI_BASE_URL,
        timeout=15.0,
        default_headers={
            "HTTP-Referer": "https://newsquawk.com",
            "X-Title": "Newsquawk SEC API",
        },
    )

# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address, default_limits=[RATE_LIMIT])

# ---------------------------------------------------------------------------
# File Paths
# ---------------------------------------------------------------------------
RUSSELL_FILE_PATH = "data/russell_share_data.csv"
CUSIP_DETAILS_FILE_PATH = "data/cusip_details_filtered_fixed.csv"
CUSIP_TO_CIK_FILE_PATH = "data/cusip_to_cik.json"
CIK_TO_TICKER_FILE_PATH = "data/cik_to_ticker.json"
TICKER_TO_CUSIP_FILE_PATH = "data/ticker_to_cusip.json"

# ---------------------------------------------------------------------------
# In-Memory Lookups & Caches
# ---------------------------------------------------------------------------
FREE_FLOAT_DATA: Dict[str, float] = {}
CUSIP_TO_CIK: Dict[str, str] = {}
CIK_TO_TICKER: Dict[str, str] = {}
TICKER_TO_CUSIP: Dict[str, any] = {}
CUSIP_TO_TICKER: Dict[str, str] = {}

COMPARE_CACHE_MAX_SIZE = 100
COMPARE_CACHE: OrderedDict[str, dict] = OrderedDict()
AI_SUMMARY_CACHE_MAX_SIZE = 200
AI_SUMMARY_CACHE: OrderedDict[str, str] = OrderedDict()

# ---------------------------------------------------------------------------
# CUSIP Details / Industry Mappings
# ---------------------------------------------------------------------------
CUSIP_DETAILS_DF = pl.DataFrame()
try:
    if os.path.exists(CUSIP_DETAILS_FILE_PATH):
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

        CUSIP_DETAILS_DF = CUSIP_DETAILS_DF.unique(subset=["cusip"], keep="first")
        logger.info(f"Loaded {CUSIP_DETAILS_DF.height} CUSIP-to-SIC mappings.")
    else:
        logger.warning(f"CUSIP details file not found: {CUSIP_DETAILS_FILE_PATH}")
except Exception as e:
    logger.warning(f"Warning: Could not load cusip_details: {e}")


def load_float_data() -> None:
    """Loads the Russell share data into memory on startup."""
    global FREE_FLOAT_DATA
    try:
        if not os.path.exists(RUSSELL_FILE_PATH):
            logger.warning(f"Russell file not found: {RUSSELL_FILE_PATH}")
            return

        df = pd.read_csv(RUSSELL_FILE_PATH)
        df = df.dropna(subset=["Symbol", "Shr Out less Closely Held Sh"])

        for _, row in df.iterrows():
            ticker = str(row["Symbol"]).strip().upper()
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


def load_json_mappings() -> None:
    """Loads CUSIP <-> CIK <-> Ticker dictionaries into memory on startup."""
    global CUSIP_TO_CIK, CIK_TO_TICKER, TICKER_TO_CUSIP, CUSIP_TO_TICKER
    try:
        if os.path.exists(CUSIP_TO_CIK_FILE_PATH):
            with open(CUSIP_TO_CIK_FILE_PATH, "r") as f:
                CUSIP_TO_CIK = json.load(f)
        if os.path.exists(CIK_TO_TICKER_FILE_PATH):
            with open(CIK_TO_TICKER_FILE_PATH, "r") as f:
                CIK_TO_TICKER = json.load(f)
        if os.path.exists(TICKER_TO_CUSIP_FILE_PATH):
            with open(TICKER_TO_CUSIP_FILE_PATH, "r") as f:
                TICKER_TO_CUSIP = json.load(f)

        for ticker, cusip_list in TICKER_TO_CUSIP.items():
            if isinstance(cusip_list, list):
                for c in cusip_list:
                    if isinstance(c, str):
                        CUSIP_TO_TICKER[c] = ticker
                        CUSIP_TO_TICKER[c.upper()] = ticker
                        CUSIP_TO_TICKER[c.lower()] = ticker
            elif isinstance(cusip_list, str):
                CUSIP_TO_TICKER[cusip_list] = ticker
                CUSIP_TO_TICKER[cusip_list.upper()] = ticker
                CUSIP_TO_TICKER[cusip_list.lower()] = ticker

        logger.info("Loaded JSON mappings for CUSIP<->CIK<->Ticker")
    except Exception as e:
        logger.error(f"Failed to load JSON mappings: {e}")


# Initialize mappings on import
load_json_mappings()
load_float_data()
