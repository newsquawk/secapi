#!/usr/bin/env python3
"""
scripts/populate_reference_prices.py

Populates and maintains the `reference_prices` table in PostgreSQL.
Calculates the peer-median price for each common stock CUSIP per reporting period
across all 13F filers. Used by `holdings_normalised` to detect and normalise
option contracts mistakenly reported in place of underlying shares (Issue 1).

Usage:
  # Populate for a specific quarter:
  python scripts/populate_reference_prices.py --period 2026-06-30

  # Populate for all recent quarters (e.g. past 2 years):
  python scripts/populate_reference_prices.py --recent 8

  # Backfill all historical quarters in the database:
  python scripts/populate_reference_prices.py --all
"""

import os
import sys
import time
import argparse
import logging
from datetime import date

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from psycopg2.extras import RealDictCursor
from database import _get_db_connection_params

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("populate_reference_prices")


def get_db_connection():
    params = _get_db_connection_params()
    conn = psycopg2.connect(**params)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0;")
    conn.commit()
    return conn


def ensure_schema_exists(conn):
    """Ensures reference_prices table and indexes exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reference_prices (
                issuer_id INT NOT NULL,
                cusip VARCHAR(9) NOT NULL,
                period_of_report DATE NOT NULL,
                median_price NUMERIC(14, 4) NOT NULL,
                sample_size INT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                PRIMARY KEY (issuer_id, period_of_report)
            );
            CREATE INDEX IF NOT EXISTS idx_ref_prices_cusip_period 
                ON reference_prices (cusip, period_of_report);
            CREATE INDEX IF NOT EXISTS idx_ref_prices_period 
                ON reference_prices (period_of_report);
        """)
    conn.commit()


def populate_period(conn, period_str: str, min_samples: int = 3) -> int:
    """
    Computes common stock and ETF median prices for a given period_of_report and
    upserts them into `reference_prices`, mapping them across all option/equity
    issuer_id variants sharing the 6-character CUSIP root.
    """
    logger.info(f"Computing reference prices for period: {period_str} (min_samples={min_samples})...")
    t0 = time.perf_counter()

    query = """
    WITH CommonMedians AS (
        SELECT 
            UPPER(i.cusip) AS cusip,
            SUBSTRING(UPPER(i.cusip) FROM 1 FOR 6) AS base6,
            ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY (
                (CASE WHEN f.filing_date < '2023-01-03'::date THEN h.value * 1000 ELSE h.value END)::numeric 
                / NULLIF(h.shares_or_principal_amount, 0)
            ))::numeric, 4) AS median_price,
            count(*) AS sample_size
        FROM holdings h
        JOIN filings f ON h.filing_id = f.filing_id
        JOIN issuers i ON h.issuer_id = i.issuer_id
        JOIN title_of_class_table tc ON h.title_of_class = tc.id
        WHERE f.period_of_report = %(period)s::date
          AND h.put_or_call = 1 -- Common stock / NONE
          AND (tc.is_common_stock = true OR tc.name IN ('TR UNIT', 'UIT EXCHANGE TRADED', 'ETF', 'UNIT', 'SHS', 'SPDR S&P 500 ETF TR', 'S&P 500 ETF TR') OR tc.name ILIKE '%%ETF%%' OR tc.name ILIKE '%%TR UNIT%%')
          AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1) -- SH only
          AND h.shares_or_principal_amount > 0
          AND h.value > 0
        GROUP BY UPPER(i.cusip)
        HAVING count(*) >= %(min_samples)s
    )
    INSERT INTO reference_prices (issuer_id, cusip, period_of_report, median_price, sample_size)
    SELECT DISTINCT ON (i.issuer_id)
        i.issuer_id,
        i.cusip,
        %(period)s::date,
        cm.median_price,
        cm.sample_size
    FROM CommonMedians cm
    JOIN issuers i ON SUBSTRING(UPPER(i.cusip) FROM 1 FOR 6) = cm.base6
    ORDER BY i.issuer_id, (UPPER(i.cusip) = cm.cusip) DESC, cm.sample_size DESC
    ON CONFLICT (issuer_id, period_of_report) DO UPDATE 
    SET median_price = EXCLUDED.median_price,
        sample_size = EXCLUDED.sample_size,
        created_at = NOW();
    """

    with conn.cursor() as cur:
        cur.execute(query, {"period": period_str, "min_samples": min_samples})
        inserted = cur.rowcount
    conn.commit()

    elapsed = time.perf_counter() - t0
    logger.info(f"Period {period_str} complete: {inserted} rows upserted in {elapsed:.2f}s.")
    return inserted


def get_available_periods(conn, limit: int = None):
    """Fetches list of distinct reporting periods from filings."""
    query = """
        SELECT DISTINCT period_of_report
        FROM filings
        WHERE form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
          AND period_of_report <= CURRENT_DATE
          AND period_of_report >= '1995-01-01'
        ORDER BY period_of_report DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
    return [r[0].strftime("%Y-%m-%d") for r in rows]


def main():
    parser = argparse.ArgumentParser(description="Populate 13F reference prices for options normalisation.")
    parser.add_argument("--period", help="Specific period_of_report to populate (YYYY-MM-DD)")
    parser.add_argument("--recent", type=int, help="Populate the N most recent quarters")
    parser.add_argument("--all", action="store_true", help="Populate all historical quarters in the database")
    parser.add_argument("--min-samples", type=int, default=3, help="Minimum filers required for median price (default: 3)")
    args = parser.parse_args()

    conn = get_db_connection()
    ensure_schema_exists(conn)

    periods_to_run = []
    if args.period:
        periods_to_run = [args.period]
    elif args.recent:
        periods_to_run = get_available_periods(conn, limit=args.recent)
    elif args.all:
        periods_to_run = get_available_periods(conn)
    else:
        # Default: run latest quarter
        periods_to_run = get_available_periods(conn, limit=1)

    logger.info(f"Targeting {len(periods_to_run)} periods: {periods_to_run}")
    total_rows = 0
    start_all = time.perf_counter()

    for p in periods_to_run:
        total_rows += populate_period(conn, p, min_samples=args.min_samples)

    total_time = time.perf_counter() - start_all
    logger.info(f"All done! Total rows upserted: {total_rows} in {total_time:.2f}s.")
    conn.close()


if __name__ == "__main__":
    main()
