#!/usr/bin/env python3
"""
scripts/detect_stock_splits.py

Detects and populates stock splits into the `stock_splits` PostgreSQL table
using cross-filer consensus from 13F filings.

Background:
When a stock executes a corporate action split (e.g. Booking Holdings 25:1,
Carvana 5:1), hundreds of institutional filers holding the stock experience the
same share multiplier and price drop across consecutive quarters.
This script detects those consensus signatures across >= 10 independent holders
and records the `split_factor` so quarter-over-quarter diffs can rebase
previous shares accurately.

Usage:
  # Detect splits for a specific reporting period:
  python scripts/detect_stock_splits.py --period 2026-06-30

  # Detect splits for the 4 most recent quarters:
  python scripts/detect_stock_splits.py --recent 4
"""

import os
import sys
import time
import argparse
import logging

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
logger = logging.getLogger("detect_stock_splits")


def get_db_connection():
    params = _get_db_connection_params()
    return psycopg2.connect(**params)


def snap_split_ratio(raw_ratio: float) -> float:
    """
    Snaps an empirical median share ratio to the nearest standard corporate
    action split ratio if within 5% tolerance, otherwise rounds to 4 decimals.
    """
    standard_ratios = [
        0.1, 0.2, 0.25, 1.0 / 3.0, 0.5,  # Reverse splits (1:10, 1:5, 1:4, 1:3, 1:2)
        2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 15.0, 20.0, 25.0, 50.0, 100.0  # Forward splits
    ]
    for std in standard_ratios:
        if abs(raw_ratio - std) / std <= 0.05:
            return round(std, 4)
    return round(raw_ratio, 4)


def get_predecessor_period(conn, period_str: str) -> str:
    """Finds the most recent 13F reporting period before the given period."""
    query = """
        SELECT DISTINCT period_of_report
        FROM filings
        WHERE form_type IN ('13F-HR', '13F-HR/A')
          AND period_of_report < %(period)s::date
        ORDER BY period_of_report DESC
        LIMIT 1;
    """
    with conn.cursor() as cur:
        cur.execute(query, {"period": period_str})
        row = cur.fetchone()
    if row:
        return row[0].strftime("%Y-%m-%d")
    return None


def detect_splits_for_period(conn, curr_period: str, prev_period: str, min_holders: int = 10) -> int:
    """
    Runs consensus detection between prev_period and curr_period.
    Upserts detected splits into `stock_splits`.
    """
    logger.info(f"Detecting splits for {curr_period} (compared to prior period {prev_period}, min_holders={min_holders})...")
    t0 = time.perf_counter()

    query = """
    WITH HolderPairs AS (
        SELECT 
            f_curr.company_id,
            UPPER(i.cusip) AS cusip,
            MIN(i.issuer_name) AS issuer_name,
            SUM(h_prev.shares_or_principal_amount) AS prev_shares,
            SUM(h_prev.value) AS prev_value,
            SUM(h_curr.shares_or_principal_amount) AS curr_shares,
            SUM(h_curr.value) AS curr_value,
            -- Share ratio (curr / prev)
            (SUM(h_curr.shares_or_principal_amount)::numeric / NULLIF(SUM(h_prev.shares_or_principal_amount), 0)) AS share_ratio,
            -- Price ratio (prev_price / curr_price)
            ((SUM(h_prev.value)::numeric / NULLIF(SUM(h_prev.shares_or_principal_amount), 0)) / 
             NULLIF((SUM(h_curr.value)::numeric / NULLIF(SUM(h_curr.shares_or_principal_amount), 0)), 0)) AS price_ratio
        FROM filings f_curr
        JOIN filings f_prev ON f_curr.company_id = f_prev.company_id 
                            AND f_prev.period_of_report = %(prev_period)s::date
                            AND f_prev.form_type IN ('13F-HR', '13F-HR/A')
        JOIN holdings h_curr ON f_curr.filing_id = h_curr.filing_id
        JOIN holdings h_prev ON f_prev.filing_id = h_prev.filing_id AND h_curr.issuer_id = h_prev.issuer_id
        JOIN issuers i ON h_curr.issuer_id = i.issuer_id
        JOIN title_of_class_table tc ON h_curr.title_of_class = tc.id AND tc.is_common_stock = true
        WHERE f_curr.period_of_report = %(curr_period)s::date
          AND f_curr.form_type IN ('13F-HR', '13F-HR/A')
          AND h_curr.put_or_call = 1 AND h_prev.put_or_call = 1
          AND (h_curr.shares_or_principal_type IS NULL OR h_curr.shares_or_principal_type = 1)
          AND (h_prev.shares_or_principal_type IS NULL OR h_prev.shares_or_principal_type = 1)
          AND h_curr.shares_or_principal_amount > 0 AND h_prev.shares_or_principal_amount > 0
          AND h_curr.value > 0 AND h_prev.value > 0
        GROUP BY f_curr.company_id, UPPER(i.cusip)
    ),
    CusipSummary AS (
        SELECT 
            cusip,
            MIN(issuer_name) AS issuer_name,
            count(*) AS holder_count,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY share_ratio)::numeric AS median_share_ratio,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY price_ratio)::numeric AS median_price_ratio
        FROM HolderPairs
        GROUP BY cusip
        HAVING count(*) >= %(min_holders)s
    )
    SELECT 
        cusip,
        issuer_name,
        holder_count,
        ROUND(median_share_ratio, 4) AS median_share_ratio,
        ROUND(median_price_ratio, 4) AS median_price_ratio
    FROM CusipSummary
    WHERE (median_price_ratio >= 1.35 OR median_price_ratio <= 0.72)
      AND (median_share_ratio >= 1.35 OR median_share_ratio <= 0.72)
      AND ABS(median_share_ratio - median_price_ratio) / NULLIF(median_price_ratio, 0) <= 0.20;
    """

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, {"curr_period": curr_period, "prev_period": prev_period, "min_holders": min_holders})
        candidates = cur.fetchall()

    if not candidates:
        logger.info(f"No splits detected for {curr_period}.")
        return 0

    inserted = 0
    with conn.cursor() as cur:
        for c in candidates:
            snapped = snap_split_ratio(float(c["median_share_ratio"]))
            logger.info(
                f"  -> Split detected: CUSIP={c['cusip']} ({c['issuer_name']}) | "
                f"Holders={c['holder_count']} | Ratio={snapped:.4f} (Raw: {c['median_share_ratio']})"
            )
            cur.execute("""
                INSERT INTO stock_splits (cusip, period_of_report, split_factor, holder_count, source, created_at)
                VALUES (%(cusip)s, %(period)s::date, %(factor)s, %(holders)s, 'consensus', NOW())
                ON CONFLICT (cusip, period_of_report) DO UPDATE
                SET split_factor = EXCLUDED.split_factor,
                    holder_count = EXCLUDED.holder_count,
                    created_at = NOW();
            """, {
                "cusip": c["cusip"],
                "period": curr_period,
                "factor": snapped,
                "holders": c["holder_count"],
            })
            inserted += 1
    conn.commit()

    elapsed = time.perf_counter() - t0
    logger.info(f"Period {curr_period} complete: {inserted} splits recorded in {elapsed:.2f}s.")
    return inserted


def get_available_periods(conn, limit: int = None):
    query = """
        SELECT DISTINCT period_of_report
        FROM filings
        WHERE form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
          AND period_of_report <= CURRENT_DATE
          AND period_of_report >= '2020-01-01'
        ORDER BY period_of_report DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
    return [r[0].strftime("%Y-%m-%d") for r in rows]


def main():
    parser = argparse.ArgumentParser(description="Detect 13F consensus stock splits across filers.")
    parser.add_argument("--period", help="Specific period_of_report to process (YYYY-MM-DD)")
    parser.add_argument("--recent", type=int, help="Process the N most recent quarters")
    parser.add_argument("--min-holders", type=int, default=10, help="Minimum dual holders (default: 10)")
    args = parser.parse_args()

    conn = get_db_connection()

    periods_to_run = []
    if args.period:
        periods_to_run = [args.period]
    elif args.recent:
        periods_to_run = get_available_periods(conn, limit=args.recent)
    else:
        periods_to_run = get_available_periods(conn, limit=2)

    total_splits = 0
    for p in periods_to_run:
        prev_p = get_predecessor_period(conn, p)
        if not prev_p:
            logger.warning(f"No predecessor period found for {p}; skipping.")
            continue
        total_splits += detect_splits_for_period(conn, p, prev_p, min_holders=args.min_holders)

    logger.info(f"All done! Total splits recorded across periods: {total_splits}.")
    conn.close()


if __name__ == "__main__":
    main()
