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
    conn = psycopg2.connect(**_get_db_connection_params())
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0;")
    conn.commit()
    return conn


def repair_companies(conn) -> None:
    """Repair corrupted legacy company AUM values."""
    print("=" * 70)
    print("Running Company AUM Repairs...")
    print("=" * 70)

    cur = conn.cursor()
    cur.execute("SET statement_timeout = 0;")

    # 1. Partial amendments (13F-HR/A)
    t0 = time.time()
    cur.execute("""
        WITH LatestFiling AS (
            SELECT DISTINCT ON (company_id)
                filing_id, company_id, form_type, filing_date, period_of_report
            FROM filings
            ORDER BY company_id, filing_date DESC, filing_id DESC
        ),
        AmendedCandidates AS (
            SELECT lf.filing_id, lf.company_id, lf.period_of_report
            FROM LatestFiling lf
            WHERE lf.form_type LIKE '%/A%'
              AND (SELECT COUNT(*) FROM holdings WHERE filing_id = lf.filing_id) < 10
        ),
        BaseFilingSamePeriod AS (
            SELECT DISTINCT ON (f.company_id)
                f.company_id, f.filing_id, f.filing_date
            FROM filings f
            JOIN AmendedCandidates ac ON f.company_id = ac.company_id
            WHERE f.period_of_report = ac.period_of_report
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
        FROM BaseHoldingSums bhs
        WHERE c.company_id = bhs.company_id
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
        WITH Pre2023Candidates AS (
            SELECT c.company_id
            FROM companies c
            WHERE c.aum > 0
              AND NOT EXISTS (
                  SELECT 1 FROM filings f
                  WHERE f.company_id = c.company_id
                    AND f.filing_date >= '2023-01-01'
              )
              AND EXISTS (
                  SELECT 1 FROM filings f
                  WHERE f.company_id = c.company_id
              )
        ),
        LatestFilings AS (
            SELECT DISTINCT ON (f.company_id)
                f.company_id, f.filing_id
            FROM filings f
            JOIN Pre2023Candidates p ON f.company_id = p.company_id
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

    # Populate amendment_type for any newly ingested 13F-HR/A filings in this period
    cur.execute("""
        WITH AmendmentData AS (
            SELECT 
                af.filing_id,
                CASE 
                    WHEN bf.filing_id IS NULL THEN 'restatement'
                    WHEN COALESCE(bf.reported_entry_total, 0) >= 50 AND COALESCE(af.reported_entry_total, 0) < 10 THEN 'addition'
                    WHEN COALESCE(bf.reported_entry_total, 0) > 0 AND (COALESCE(af.reported_entry_total, 0)::numeric / COALESCE(bf.reported_entry_total, 0)) < 0.25 THEN 'addition'
                    WHEN af.reported_value_total IS NOT NULL AND bf.reported_value_total IS NOT NULL AND bf.reported_value_total > 0 AND (af.reported_value_total::numeric / bf.reported_value_total) < 0.25 AND COALESCE(af.reported_entry_total, 0) < 0.5 * COALESCE(bf.reported_entry_total, 0) THEN 'addition'
                    ELSE 'restatement'
                END AS derived_amendment_type
            FROM filings af
            LEFT JOIN LATERAL (
                SELECT filing_id, reported_entry_total, reported_value_total
                FROM filings bf
                WHERE bf.company_id = af.company_id
                  AND bf.period_of_report = af.period_of_report
                  AND bf.form_type = '13F-HR'
                ORDER BY bf.filing_date DESC, bf.filing_id DESC
                LIMIT 1
            ) bf ON TRUE
            WHERE af.period_of_report = %s
              AND af.form_type LIKE '%%/A%%'
              AND af.amendment_type IS NULL
        )
        UPDATE filings f
        SET amendment_type = ad.derived_amendment_type
        FROM AmendmentData ad
        WHERE f.filing_id = ad.filing_id;
    """, (period,))

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
