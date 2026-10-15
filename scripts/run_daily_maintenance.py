#!/usr/bin/env python3
"""
scripts/run_daily_maintenance.py

Unified operational automation pipeline for secapi:
1. Populates per-filing totals (reported_value_total & reported_entry_total)
   for any newly ingested filings that are missing totals.
2. Refreshes peer-median reference prices for current and recent reporting
   periods (incorporating newly submitted filings and synthetic CUSIP options/ETFs).
3. Detects consensus stock splits across filers for current and recent
   reporting periods.

Concurrency:
Protected by a PostgreSQL advisory lock (key: 987654321) so that concurrent
invocations (e.g. cron overlap or simultaneous API trigger) cleanly exit
without lock contention or race conditions.

Usage:
  # Standard daily maintenance (processes unpopulated filings + 2 recent quarters):
  python scripts/run_daily_maintenance.py

  # Process the 4 most recent quarters:
  python scripts/run_daily_maintenance.py --recent 4

  # Dry-run inspection:
  python scripts/run_daily_maintenance.py --dry-run
"""

import os
import sys
import time
import argparse
import logging
from typing import Dict, Any

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from database import _get_db_connection_params
from scripts.populate_filing_totals import populate_period as populate_filing_total_period
from scripts.populate_reference_prices import (
    ensure_schema_exists as ensure_ref_prices_schema,
    populate_period as populate_ref_price_period,
    get_available_periods as get_ref_price_periods,
)
from scripts.detect_stock_splits import (
    detect_splits_for_period,
    get_predecessor_period,
    get_available_periods as get_split_periods,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_daily_maintenance")

ADVISORY_LOCK_ID = 987654321


def get_db_connection():
    params = _get_db_connection_params()
    conn = psycopg2.connect(**params)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0;")
    conn.commit()
    return conn


def acquire_advisory_lock(conn) -> bool:
    """Tries to acquire a session-level advisory lock. Returns False if already held."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s);", (ADVISORY_LOCK_ID,))
        acquired = cur.fetchone()[0]
    return bool(acquired)


def release_advisory_lock(conn) -> None:
    """Releases the session-level advisory lock."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s);", (ADVISORY_LOCK_ID,))
        conn.commit()
    except Exception as e:
        logger.warning(f"Error releasing advisory lock: {e}")


def run_maintenance(
    recent_quarters: int = 2,
    skip_filing_totals: bool = False,
    skip_ref_prices: bool = False,
    skip_splits: bool = False,
    all_periods: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Executes the full automated maintenance pipeline.
    Returns a dictionary of execution statistics.
    """
    stats = {
        "status": "success",
        "lock_acquired": False,
        "filings_populated": 0,
        "ref_prices_upserted": 0,
        "splits_recorded": 0,
        "elapsed_seconds": 0.0,
        "message": "",
    }

    t0 = time.perf_counter()
    conn = get_db_connection()

    try:
        if not acquire_advisory_lock(conn):
            msg = "Maintenance job already running (advisory lock held). Skipping this invocation."
            logger.warning(msg)
            stats["status"] = "skipped"
            stats["message"] = msg
            return stats

        stats["lock_acquired"] = True
        logger.info("=" * 65)
        logger.info("STARTING DAILY MAINTENANCE PIPELINE")
        logger.info("=" * 65)

        # ---------------------------------------------------------------------
        # 1. Populate Filing Totals for Unpopulated Filings
        # ---------------------------------------------------------------------
        if not skip_filing_totals:
            logger.info("--- [Phase 1/3] Populating Filing Totals ---")
            totals_query = """
                SELECT DISTINCT period_of_report
                FROM filings
                WHERE reported_value_total IS NULL
                  AND period_of_report IS NOT NULL
                  AND period_of_report <= CURRENT_DATE
            """
            if not all_periods:
                # By default, focus on recent filings (last 365 days or recent ingestion)
                totals_query += " AND (period_of_report >= (CURRENT_DATE - INTERVAL '365 days') OR created_at >= (NOW() - INTERVAL '30 days'))"
            totals_query += " ORDER BY period_of_report DESC;"

            with conn.cursor() as cur:
                cur.execute(totals_query)
                unpopulated_periods = [row[0] for row in cur.fetchall()]

            if unpopulated_periods:
                logger.info(f"Found {len(unpopulated_periods)} periods with unpopulated filings: {unpopulated_periods}")
                for p in unpopulated_periods:
                    updated = populate_filing_total_period(conn, p, dry_run=dry_run)
                    stats["filings_populated"] += updated
            else:
                logger.info("All active filings currently have reported_value_total populated. Nothing to update.")
        else:
            logger.info("--- [Phase 1/3] Populating Filing Totals: SKIPPED ---")

        # ---------------------------------------------------------------------
        # 2. Refresh Reference Prices
        # ---------------------------------------------------------------------
        if not skip_ref_prices:
            logger.info("--- [Phase 2/3] Refreshing Peer Reference Prices ---")
            ensure_ref_prices_schema(conn)
            periods = get_ref_price_periods(conn, limit=recent_quarters)
            logger.info(f"Targeting {len(periods)} recent periods for reference prices: {periods}")
            for p in periods:
                if not dry_run:
                    upserted = populate_ref_price_period(conn, p)
                    stats["ref_prices_upserted"] += upserted
                else:
                    logger.info(f"[DRY-RUN] Would recompute reference prices for {p}")
        else:
            logger.info("--- [Phase 2/3] Refreshing Reference Prices: SKIPPED ---")

        # ---------------------------------------------------------------------
        # 3. Detect Consensus Stock Splits
        # ---------------------------------------------------------------------
        if not skip_splits:
            logger.info("--- [Phase 3/3] Detecting Consensus Stock Splits ---")
            split_target_periods = get_split_periods(conn, limit=recent_quarters)
            logger.info(f"Targeting {len(split_target_periods)} periods for split detection: {split_target_periods}")
            for p in split_target_periods:
                prev_p = get_predecessor_period(conn, p)
                if not prev_p:
                    logger.info(f"No prior period found for {p}; skipping.")
                    continue
                if not dry_run:
                    splits = detect_splits_for_period(conn, p, prev_p)
                    stats["splits_recorded"] += splits
                else:
                    logger.info(f"[DRY-RUN] Would detect splits between {prev_p} and {p}")
        else:
            logger.info("--- [Phase 3/3] Detecting Stock Splits: SKIPPED ---")

        stats["elapsed_seconds"] = round(time.perf_counter() - t0, 2)
        stats["message"] = (
            f"Daily maintenance completed in {stats['elapsed_seconds']}s. "
            f"Filings populated: {stats['filings_populated']} | "
            f"Ref prices upserted: {stats['ref_prices_upserted']} | "
            f"Splits recorded: {stats['splits_recorded']}."
        )
        logger.info("=" * 65)
        logger.info(stats["message"])
        logger.info("=" * 65)

    except Exception as e:
        logger.error(f"Error during daily maintenance: {e}", exc_info=True)
        stats["status"] = "error"
        stats["message"] = str(e)
        stats["elapsed_seconds"] = round(time.perf_counter() - t0, 2)
    finally:
        if stats["lock_acquired"]:
            release_advisory_lock(conn)
        conn.close()

    return stats


def main():
    parser = argparse.ArgumentParser(description="Run automated SEC API daily maintenance pipeline.")
    parser.add_argument("--recent", type=int, default=2, help="Number of recent quarters to refresh (default: 2)")
    parser.add_argument("--all", action="store_true", help="Process all historical unpopulated periods back to 1995")
    parser.add_argument("--skip-filing-totals", action="store_true", help="Skip populating missing filing totals")
    parser.add_argument("--skip-ref-prices", action="store_true", help="Skip refreshing reference prices")
    parser.add_argument("--skip-splits", action="store_true", help="Skip stock split detection")
    parser.add_argument("--dry-run", action="store_true", help="Inspect what would run without committing writes")
    args = parser.parse_args()

    run_maintenance(
        recent_quarters=args.recent,
        skip_filing_totals=args.skip_filing_totals,
        skip_ref_prices=args.skip_ref_prices,
        skip_splits=args.skip_splits,
        all_periods=args.all,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
