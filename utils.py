import concurrent.futures
from typing import Optional, Tuple
import psycopg2
import yfinance as yf

from config import (
    FREE_FLOAT_DATA,
    CUSIP_TO_CIK,
    CIK_TO_TICKER,
    TICKER_TO_CUSIP,
    CUSIP_TO_TICKER,
    logger,
)


def resolve_identifiers(
    identifier: str, db: psycopg2.extensions.cursor
) -> Tuple[Optional[str], Optional[str]]:
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

        # 1. Try reverse lookup using global dict
        if cusip in CUSIP_TO_TICKER:
            ticker = CUSIP_TO_TICKER[cusip]

        # 2. If no ticker yet, try CUSIP -> CIK -> Ticker (JSON lists)
        if not ticker:
            cik_list = CUSIP_TO_CIK.get(cusip)
            if cik_list and isinstance(cik_list, list) and len(cik_list) > 0:
                cik_str = str(cik_list[0])
                ticker_list = CIK_TO_TICKER.get(cik_str)
                if (
                    ticker_list
                    and isinstance(ticker_list, list)
                    and len(ticker_list) > 0
                ):
                    ticker = ticker_list[0].upper()

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


def get_free_float(ticker: str) -> Optional[float]:
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

        # Enforce 5-second timeout to prevent worker thread starvation
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: stock.info.get("floatShares"))
            float_shares_raw = future.result(timeout=5.0)

        if float_shares_raw:
            float_in_millions = float_shares_raw / 1_000_000.0
            FREE_FLOAT_DATA[ticker] = float_in_millions
            logger.info(
                f"Successfully fetched and cached float for {ticker}: {float_in_millions}M"
            )
            return float_in_millions
        else:
            logger.warning(f"Yahoo Finance has no float data for {ticker}. Caching None.")
            FREE_FLOAT_DATA[ticker] = None
            return None

    except Exception as e:
        logger.error(f"Error fetching float for {ticker} via yfinance: {e}. Caching None.")
        FREE_FLOAT_DATA[ticker] = None
        return None
