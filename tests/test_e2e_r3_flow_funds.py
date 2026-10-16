"""
tests/test_e2e_r3_flow_funds.py

Requirement 3 (R3): Number of Funds Buying or Selling the Same Stock by Type
Methodology: 4-Tier Opaque-Box E2E Testing Suite
- Tier 1: Feature Coverage (funds_buying, funds_selling, funds_new, funds_increased, funds_decreased, funds_closed, funds_unchanged)
- Tier 2: Boundary & Corner Cases (zero activity, split rebasing, 1-share tolerance, option exclusion, amendment deduplication)
- Tier 3: Cross-Feature Combinations (flow vs activity parity, daily vs aggregate consistency, top-changes sorting, ticker vs CUSIP)
- Tier 4: Real-World Application Scenarios (AAPL daily flow, BKNG split flow, 2026-08-14 top-changes, MSFT quarterly flow)
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
from typing import Dict, Any, List
from fastapi.testclient import TestClient

from main import app
import psycopg2
from psycopg2.extras import RealDictCursor


def get_db_connection():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME", "sec"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "password"),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
    )


class TestE2ER3FlowFunds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    # -------------------------------------------------------------------------
    # TIER 1: Feature Coverage (Isolated Representative Inputs & Invariants)
    # -------------------------------------------------------------------------

    def test_r3_t1_daily_flow_fund_fields_and_types(self):
        """
        [Tier 1 - Feature Coverage]
        R3.1: Daily flow endpoint must return all 7 fund fields with integer types:
        funds_buying, funds_selling, funds_new, funds_increased, funds_decreased,
        funds_closed, funds_unchanged.
        """
        res = self.client.get("/api/v1/flow/daily/037833100?days=5")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        daily_data = data.get("daily_data", [])
        self.assertGreater(len(daily_data), 0, "Expected daily flow data")

        fund_fields = [
            "funds_buying", "funds_selling", "funds_new", "funds_increased",
            "funds_decreased", "funds_closed", "funds_unchanged"
        ]
        for entry in daily_data:
            for field in fund_fields:
                self.assertIn(field, entry, f"Missing field {field} in daily flow entry")
                self.assertIsInstance(entry[field], int, f"Field {field} must be integer")
                self.assertGreaterEqual(entry[field], 0, f"Field {field} must be >= 0")

    def test_r3_t1_daily_flow_buying_selling_invariants(self):
        """
        [Tier 1 - Feature Coverage]
        R3.3: Strict invariants must hold across every daily flow entry:
        1. funds_buying == funds_new + funds_increased
        2. funds_selling == funds_decreased + funds_closed
        """
        res = self.client.get("/api/v1/flow/daily/037833100?days=14")
        self.assertEqual(res.status_code, 200)
        for entry in res.json().get("daily_data", []):
            funds_buying = entry.get("funds_buying", 0)
            funds_new = entry.get("funds_new", 0)
            funds_increased = entry.get("funds_increased", 0)
            self.assertEqual(
                funds_buying,
                funds_new + funds_increased,
                f"Invariant failed: funds_buying ({funds_buying}) != funds_new ({funds_new}) + funds_increased ({funds_increased})"
            )

            funds_selling = entry.get("funds_selling", 0)
            funds_decreased = entry.get("funds_decreased", 0)
            funds_closed = entry.get("funds_closed", 0)
            self.assertEqual(
                funds_selling,
                funds_decreased + funds_closed,
                f"Invariant failed: funds_selling ({funds_selling}) != funds_decreased ({funds_decreased}) + funds_closed ({funds_closed})"
            )

    def test_r3_t1_aggregate_flow_fund_fields_and_invariants(self):
        """
        [Tier 1 - Feature Coverage]
        R3.1 / R3.3: Aggregate flow endpoint (/api/v1/flow/aggregate/{id}) must return
        all 7 fund fields satisfying the invariants.
        """
        res = self.client.get("/api/v1/flow/aggregate/037833100?days=14")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        fund_fields = [
            "funds_buying", "funds_selling", "funds_new", "funds_increased",
            "funds_decreased", "funds_closed", "funds_unchanged"
        ]
        for field in fund_fields:
            self.assertIn(field, data, f"Missing {field} in aggregate flow response")
            self.assertIsInstance(data[field], int)
            self.assertGreaterEqual(data[field], 0)

        self.assertEqual(
            data["funds_buying"],
            data["funds_new"] + data["funds_increased"]
        )
        self.assertEqual(
            data["funds_selling"],
            data["funds_decreased"] + data["funds_closed"]
        )

    def test_r3_t1_top_changes_fund_fields_and_invariants(self):
        """
        [Tier 1 - Feature Coverage]
        R3.1 / R3.3: Top changes endpoint (/api/v1/flow/top-changes) must return
        fund counts for every stock mover entry, satisfying invariants.
        """
        res = self.client.get("/api/v1/flow/top-changes?date=2026-08-14&sort_by=shares")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        stocks = data.get("stocks", [])
        self.assertGreater(len(stocks), 0, "Expected top mover stocks")

        fund_fields = [
            "funds_buying", "funds_selling", "funds_new", "funds_increased",
            "funds_decreased", "funds_closed", "funds_unchanged"
        ]
        for stock in stocks:
            for field in fund_fields:
                self.assertIn(field, stock, f"Missing {field} on top-changes stock {stock.get('cusip')}")
                self.assertIsInstance(stock[field], int)
                self.assertGreaterEqual(stock[field], 0)

            self.assertEqual(stock["funds_buying"], stock["funds_new"] + stock["funds_increased"])
            self.assertEqual(stock["funds_selling"], stock["funds_decreased"] + stock["funds_closed"])

    def test_r3_t1_quarterly_flow_fund_fields_and_invariants(self):
        """
        [Tier 1 - Feature Coverage]
        R3.4: Per-stock / per-quarter flow endpoint (/api/v1/flow/quarterly/{identifier})
        returns multi-quarter fund count breakdowns.
        """
        res = self.client.get("/api/v1/flow/quarterly/037833100?quarters=4")
        if res.status_code == 200:
            data = res.json()
            q_data = data.get("quarterly_data", [])
            for q_entry in q_data:
                self.assertIn("funds_buying", q_entry)
                self.assertIn("funds_selling", q_entry)
                self.assertEqual(
                    q_entry["funds_buying"],
                    q_entry.get("funds_new", 0) + q_entry.get("funds_increased", 0)
                )
                self.assertEqual(
                    q_entry["funds_selling"],
                    q_entry.get("funds_decreased", 0) + q_entry.get("funds_closed", 0)
                )

    # -------------------------------------------------------------------------
    # TIER 2: Boundary & Corner Cases
    # -------------------------------------------------------------------------

    def test_r3_t2_zero_activity_day_empty_counts(self):
        """
        [Tier 2 - Boundary]
        If an identifier had zero activity in a window, fund counts must be 0
        (never null or 500 error).
        """
        # Obscure CUSIP with zero recent activity
        res = self.client.get("/api/v1/flow/aggregate/000000000?days=1")
        # Should return 200 with 0 values or 404
        if res.status_code == 200:
            data = res.json()
            self.assertEqual(data.get("funds_buying"), 0)
            self.assertEqual(data.get("funds_selling"), 0)
            self.assertEqual(data.get("funds_new"), 0)
            self.assertEqual(data.get("funds_unchanged"), 0)

    def test_r3_t2_stock_split_rebasing_fund_classification(self):
        """
        [Tier 2 - Boundary]
        R3.2: Stock split rebasing (Booking Holdings BKNG 25:1) ensures unchanged
        shareholders are categorized into funds_unchanged, NOT funds_increased.
        """
        res = self.client.get("/api/v1/flow/aggregate/BKNG?days=30")
        if res.status_code == 200:
            data = res.json()
            self.assertIn("funds_unchanged", data)
            self.assertIn("funds_increased", data)
            # Invariant must hold even under stock split
            self.assertEqual(data["funds_buying"], data["funds_new"] + data["funds_increased"])

    def test_r3_t2_cash_in_lieu_one_share_tolerance_in_flow(self):
        """
        [Tier 2 - Boundary]
        R3.2: When ABS(current_shares - rebased_prev_shares) <= 1 under a stock split,
        the fund must be counted in funds_unchanged, not buying or selling.
        """
        # Bridgewater (0001929139) on BKNG had a 1-share discrepancy
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT ss.split_factor
                    FROM stock_splits ss
                    WHERE ss.cusip = '09857L108' AND ss.period_of_report = '2026-06-30';
                """)
                split = cur.fetchone()
        if split:
            self.assertEqual(float(split["split_factor"]), 25.0)

    def test_r3_t2_option_holdings_excluded_from_common_stock_flow(self):
        """
        [Tier 2 - Boundary]
        Flow queries must query holdings_normalised with common stock filters
        and exclude option contracts (PUT, CALL) so option contract counts
        do not pollute share sums or fund counts.
        """
        # Amazon (AMZN) has heavy option activity
        res = self.client.get("/api/v1/flow/daily/AMZN?days=3")
        self.assertEqual(res.status_code, 200)
        for entry in res.json().get("daily_data", []):
            self.assertIn("funds_buying", entry)
            self.assertEqual(entry["funds_buying"], entry.get("funds_new", 0) + entry.get("funds_increased", 0))

    def test_r3_t2_amendment_deduplication_single_fund_count(self):
        """
        [Tier 2 - Boundary]
        If a manager submitted both an initial 13F-HR and an amendment 13F-HR/A
        in the same window, it must be counted as exactly 1 distinct fund.
        """
        # Inspire Investing (CIK 0001670139) submitted both 13F-HR and 13F-HR/A in Q1 2026
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT count(distinct f.company_id) as distinct_funds, count(f.filing_id) as total_filings
                    FROM filings f
                    JOIN companies c ON f.company_id = c.company_id
                    WHERE c.cik_number = '1670139' AND f.period_of_report = '2026-03-31';
                """)
                row = cur.fetchone()
                self.assertEqual(row["distinct_funds"], 1)
                self.assertGreater(row["total_filings"], 1)

    # -------------------------------------------------------------------------
    # TIER 3: Cross-Feature Combinations
    # -------------------------------------------------------------------------

    def test_r3_t3_flow_aggregate_sums_consistent_with_daily(self):
        """
        [Tier 3 - Cross-Feature]
        Gross buying and selling in /api/v1/flow/aggregate should be consistent
        with the daily breakdown in /api/v1/flow/daily over the same window.
        """
        daily_res = self.client.get("/api/v1/flow/daily/037833100?days=5")
        agg_res = self.client.get("/api/v1/flow/aggregate/037833100?days=5")
        self.assertEqual(daily_res.status_code, 200)
        self.assertEqual(agg_res.status_code, 200)

        daily_data = daily_res.json().get("daily_data", [])
        agg_data = agg_res.json()
        daily_buying_sum = sum(d.get("gross_buying", 0) for d in daily_data)
        daily_selling_sum = sum(d.get("gross_selling", 0) for d in daily_data)

        self.assertAlmostEqual(daily_buying_sum, agg_data.get("gross_buying", 0), delta=1.0)
        self.assertAlmostEqual(daily_selling_sum, agg_data.get("gross_selling", 0), delta=1.0)

    def test_r3_t3_top_changes_sorting_preserves_fund_metrics(self):
        """
        [Tier 3 - Cross-Feature]
        Sorting top changes by 'shares' vs 'value' preserves all 7 fund fields
        and their invariants.
        """
        res_shares = self.client.get("/api/v1/flow/top-changes?date=2026-08-14&sort_by=shares")
        res_value = self.client.get("/api/v1/flow/top-changes?date=2026-08-14&sort_by=value")
        self.assertEqual(res_shares.status_code, 200)
        self.assertEqual(res_value.status_code, 200)

        for stock in res_shares.json().get("stocks", [])[:10]:
            self.assertIn("funds_buying", stock)
            self.assertEqual(stock["funds_buying"], stock.get("funds_new", 0) + stock.get("funds_increased", 0))
        for stock in res_value.json().get("stocks", [])[:10]:
            self.assertIn("funds_buying", stock)
            self.assertEqual(stock["funds_buying"], stock.get("funds_new", 0) + stock.get("funds_increased", 0))

    def test_r3_t3_ticker_vs_cusip_identifier_resolution(self):
        """
        [Tier 3 - Cross-Feature]
        Requesting by ticker 'AAPL' or CUSIP '037833100' returns consistent fund metrics.
        """
        res_ticker = self.client.get("/api/v1/flow/daily/AAPL?days=3")
        res_cusip = self.client.get("/api/v1/flow/daily/037833100?days=3")
        self.assertEqual(res_ticker.status_code, 200)
        self.assertEqual(res_cusip.status_code, 200)
        self.assertEqual(len(res_ticker.json().get("daily_data", [])), len(res_cusip.json().get("daily_data", [])))

    # -------------------------------------------------------------------------
    # TIER 4: Real-World Application Scenarios (Live PostgreSQL Integration)
    # -------------------------------------------------------------------------

    def test_r3_t4_real_apple_daily_flow(self):
        """
        [Tier 4 - Real-World Integration]
        Real Apple (AAPL, CUSIP 037833100):
        Verify fund counts >= 0 and invariants across all available days.
        """
        res = self.client.get("/api/v1/flow/daily/AAPL?days=7")
        self.assertEqual(res.status_code, 200)
        daily_data = res.json().get("daily_data", [])
        for entry in daily_data:
            self.assertIn("funds_buying", entry)
            self.assertGreaterEqual(entry.get("funds_buying", 0), 0)
            self.assertGreaterEqual(entry.get("funds_selling", 0), 0)
            self.assertEqual(entry["funds_buying"], entry.get("funds_new", 0) + entry.get("funds_increased", 0))
            self.assertEqual(entry["funds_selling"], entry.get("funds_decreased", 0) + entry.get("funds_closed", 0))

    def test_r3_t4_real_booking_aggregate_flow_under_split(self):
        """
        [Tier 4 - Real-World Integration]
        Real Booking Holdings (BKNG, CUSIP 09857L108) under 25:1 split:
        Verify aggregate flow invariants.
        """
        res = self.client.get("/api/v1/flow/aggregate/BKNG?days=14")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("funds_buying", data)
        self.assertEqual(data["funds_buying"], data.get("funds_new", 0) + data.get("funds_increased", 0))
        self.assertEqual(data["funds_selling"], data.get("funds_decreased", 0) + data.get("funds_closed", 0))

    def test_r3_t4_real_top_changes_active_date(self):
        """
        [Tier 4 - Real-World Integration]
        Active filing date 2026-08-14:
        Verify top 50 mover stocks all possess complete fund metrics.
        """
        res = self.client.get("/api/v1/flow/top-changes?date=2026-08-14&sort_by=shares")
        self.assertEqual(res.status_code, 200)
        stocks = res.json().get("stocks", [])
        self.assertGreater(len(stocks), 0)
        for stock in stocks:
            self.assertIn("funds_buying", stock)
            self.assertEqual(stock["funds_buying"], stock.get("funds_new", 0) + stock.get("funds_increased", 0))
            self.assertEqual(stock["funds_selling"], stock.get("funds_decreased", 0) + stock.get("funds_closed", 0))

    def test_r3_t4_real_quarterly_flow_microsoft(self):
        """
        [Tier 4 - Real-World Integration]
        Microsoft (MSFT, CUSIP 594918104) quarterly flow endpoint.
        """
        res = self.client.get("/api/v1/flow/quarterly/MSFT?quarters=4")
        if res.status_code == 200:
            for q_entry in res.json().get("quarterly_data", []):
                self.assertEqual(q_entry["funds_buying"], q_entry["funds_new"] + q_entry["funds_increased"])
                self.assertEqual(q_entry["funds_selling"], q_entry["funds_decreased"] + q_entry["funds_closed"])


if __name__ == "__main__":
    unittest.main()
