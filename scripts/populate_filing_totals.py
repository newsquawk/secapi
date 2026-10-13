#!/usr/bin/env python3
"""
Populate `reported_value_total` and `reported_entry_total` on `filings`.

This operational script processes filings period-by-period (quarter-by-quarter)
to calculate and persist the accurate per-filing portfolio value total and holding
count without taking long table locks.

Usage:
    # Populate the most recent 4 quarters (default):
    python scripts/populate_filing_totals.py --recent 4

    # Populate a specific quarter:
    python scripts/populate_filing_totals.py --period 2026-06-30

    # Populate all historical quarters:
    python scripts/populate_filing_totals.py --all

    # Run company AUM repairs (pre-2023 thousands and partial amendment clobbers):
    python scripts/populate_filing_totals.py --repair-companies
"""

import argparse
import os
import sys
import time
from datetime import date
from typing import List, Optional

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from psycopg2.extras import RealDictCursor

# Import DB connection parameters from project database module
from database import _get_db_connection_params
from config import logger


def get_connection():
    return psycopg2.connect(**_get_db_connection_params())


def repair_companies(conn) -> None:
    """Repair corrupted legacy company AUM values."""
    print("=" * 70)
    print("Running Company AUM Repairs...")
    print("=" * 70)

    cur = conn.cursor()

    # 1. Partial amendments (13F-HR/A)
    t0 = time.time()
    cur.execute("""
        WITH LatestFiling AS (
            SELECT DISTINCT ON (company_id)
                filing_id, company_id, form_type, filing_date, period_of_report
            FROM filings
            ORDER BY company_id, filing_date DESC, filing_id DESC
        ),
        BaseFilingSamePeriod AS (
            SELECT DISTINCT ON (f.company_id)
                f.company_id, f.filing_id, f.form_type, f.filing_date, f.period_of_report
            FROM filings f
            JOIN LatestFiling lf ON f.company_id = lf.company_id
            WHERE f.period_of_report = lf.period_of_report
              AND f.form_type = '13F-HR'
            ORDER BY f.company_id, f.filing_date DESC, f.filing_id DESC
        ),
        BaseHoldingSums AS (
            SELECT
                bf.company_id,
                SUM(CASE WHEN bf.filing_date < '2023-01-03' THEN h.value * 1000 ELSE h.value END) AS base_portfolio_val
            FROM BaseFilingSamePeriod bf
            JOIN holdings h ON bf.filing_id = h.filing_id
            GROUP BY bf.company_id
            HAVING COUNT(h.holding_id) >= 50
        )
        UPDATE companies c
        SET aum = bhs.base_portfolio_val
        FROM LatestFiling lf
        JOIN BaseHoldingSums bhs ON lf.company_id = bhs.company_id
        WHERE c.company_id = lf.company_id
          AND lf.form_type LIKE '%/A%'
          AND (SELECT COUNT(*) FROM holdings WHERE filing_id = lf.filing_id) < 10
          AND bhs.base_portfolio_val > c.aum * 2
        RETURNING c.company_name, c.aum;
    """)
    amended_repaired = cur.fetchall()
    conn.commit()
    print(f"Repaired {len(amended_repaired)} companies corrupted by partial amendments ({time.time() - t0:.2f}s):")
    for name, aum in amended_repaired:
        print(f"  - {name}: ${aum:,}")

    # 2. Pre-2023 companies stored in thousands
    t0 = time.time()
    cur.execute("""
        WITH Pre2023Companies AS (
            SELECT company_id
            FROM filings
            GROUP BY company_id
            HAVING MAX(filing_date) < '2023-01-01'
        ),
        LatestFilings AS (
            SELECT DISTINCT ON (f.company_id)
                f.company_id, f.filing_id
            FROM filings f
            JOIN Pre2023Companies p ON f.company_id = p.company_id
            ORDER BY f.company_id, f.filing_date DESC, f.filing_id DESC
        ),
        NormSums AS (
            SELECT
                lf.company_id,
                SUM(h.value * 1000) AS norm_sum
            FROM LatestFilings lf
            JOIN holdings h ON lf.filing_id = h.filing_id
            GROUP BY lf.company_id
        )
        UPDATE companies c
        SET aum = ns.norm_sum
        FROM NormSums ns
        WHERE c.company_id = ns.company_id
          AND c.aum < ns.norm_sum / 500
        RETURNING c.company_name, c.aum;
    """)
    pre2023_repaired = cur.fetchall()
    conn.commit()
    print(f"\nRepaired {len(pre2023_repaired)} pre-2023 companies stored in thousands ({time.time() - t0:.2f}s). Sample 5:")
    for name, aum in pre2023_repaired[:5]:
        print(f"  - {name}: ${aum:,}")


def get_reporting_periods(conn, all_periods: bool = False, limit_recent: Optional[int] = None) -> List[date]:
    """Retrieve list of distinct reporting periods."""
    cur = conn.cursor()
    query = """
        SELECT period_of_report
        FROM filings
        WHERE period_of_report IS NOT NULL
        GROUP BY period_of_report
        ORDER BY period_of_report DESC
    """
    if not all_periods and limit_recent:
        query += f" LIMIT {int(limit_recent)}"
    cur.execute(query)
    return [row[0] for row in cur.fetchall()]


def populate_period(conn, period: date, dry_run: bool = False) -> int:
    """Populate filings.reported_value_total and reported_entry_total for a single period."""
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*)
        FROM filings
        WHERE period_of_report = %s AND reported_value_total IS NULL
    """, (period,))
    unpopulated_count = cur.fetchone()[0]

    if unpopulated_count == 0:
        print(f"[{period}] All filings already have reported_value_total populated. Skipping.")
        return 0

    print(f"[{period}] Found {unpopulated_count} filings to populate...")
    if dry_run:
        return unpopulated_count

    t0 = time.time()
    # Batch update filings that have holdings
    cur.execute("""
        UPDATE filings f
        SET reported_value_total = sub.val,
            reported_entry_total = sub.cnt
        FROM (
            SELECT
                h.filing_id,
                COUNT(h.holding_id) AS cnt,
                SUM(CASE WHEN f2.filing_date < '2023-01-03' THEN h.value * 1000 ELSE h.value END) AS val
            FROM holdings h
            JOIN filings f2 ON h.filing_id = f2.filing_id
            WHERE f2.period_of_report = %s
              AND f2.reported_value_total IS NULL
            GROUP BY h.filing_id
        ) sub
        WHERE f.filing_id = sub.filing_id;
    """, (period,))
    updated_with_holdings = cur.rowcount

    # Set 0 for filings with 0 holdings (e.g. notices/13F-NT)
    cur.execute("""
        UPDATE filings
        SET reported_value_total = 0,
            reported_entry_total = 0
        WHERE period_of_report = %s
          AND reported_value_total IS NULL;
    """, (period,))
    updated_zeros = cur.rowcount

    conn.commit()
    elapsed = time.time() - t0
    total_updated = updated_with_holdings + updated_zeros
    print(f"[{period}] Finished: updated {total_updated} filings ({updated_with_holdings} with holdings, {updated_zeros} empty/notices) in {elapsed:.2f}s.")
    return total_updated


def main():
    parser = argparse.ArgumentParser(description="Populate filing portfolio value totals and repair legacy AUM data.")
    parser.add_argument("--period", type=str, help="Populate a specific reporting period (YYYY-MM-DD)")
    parser.add_argument("--recent", type=int, default=None, help="Populate the N most recent reporting periods (default 4)")
    parser.add_argument("--all", action="store_true", help="Populate all reporting periods in history")
    parser.add_argument("--repair-companies", action="store_true", help="Execute company AUM repairs")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without applying updates")

    args = parser.parse_args()

    # Default to 4 recent periods if no specific mode selected
    if not args.period and not args.recent and not args.all and not args.repair_companies:
        args.recent = 4

    conn = get_connection()

    try:
        if args.repair_companies:
            repair_companies(conn)

        periods_to_process = []
        if args.period:
            periods_to_process = [date.fromisoformat(args.period)]
        elif args.all:
            periods_to_process = get_reporting_periods(conn, all_periods=True)
        elif args.recent:
            periods_to_process = get_reporting_periods(conn, limit_recent=args.recent)

        if periods_to_process:
            print("\n" + "=" * 70)
            print(f"Populating Filing Totals for {len(periods_to_process)} periods...")
            print("=" * 70)
            grand_total = 0
            start_all = time.time()
            for p in periods_to_process:
                grand_total += populate_period(conn, p, dry_run=args.dry_run)
            print("=" * 70)
            print(f"Done. Processed {len(periods_to_process)} periods, {grand_total} filings updated in {time.time() - start_all:.2f}s.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
