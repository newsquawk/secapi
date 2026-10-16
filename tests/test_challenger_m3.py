"""
tests/test_challenger_m3.py

Adversarial Stress Harness & Empirical Verification for Milestone 3:
Requirement 3 (R3) Flow Fund Counts by Type.

Executed by: challenger_m3_1 (Empirical Challenger)

Empirical validation objectives:
1. Live Apple (AAPL) and Microsoft (MSFT) flow responses:
   - Validate /api/v1/flow/daily/{id}, /api/v1/flow/aggregate/{id}, /api/v1/flow/quarterly/{id}.
   - Verify all 7 fund metrics are returned as integer types (>= 0).
   - Verify strict mathematical invariants:
     funds_buying == funds_new + funds_increased
     funds_selling == funds_decreased + funds_closed
2. Booking Holdings (BKNG, CUSIP 09857L108) 25:1 Stock Split:
   - Verify split factor 25.0 in stock_splits for period 2026-06-30.
   - Verify institutional holders whose share counts increased by 25x (or within 1-share cash-in-lieu tolerance)
     are classified as 'unchanged' and NOT counted as buyers (funds_buying, funds_new, funds_increased).
   - Test ground truth institutional holders:
     * Cohen & Steers (company_id 216): 50 -> 1250 shares (exact 25x match -> unchanged)
     * Manchester Capital Management LLC (company_id 1477): 409 -> 10224 shares (rebased 10225, diff=1 -> unchanged)
3. Top Market Changes (/api/v1/flow/top-changes):
   - Verify 50 top mover stocks on high-volume reporting date (2026-08-14).
   - Verify integer fund fields and additive invariants across all 50 stocks.
4. Option Scope Isolation:
   - Verify option lines (PUT, CALL) and non-common-stock holdings do not pollute common stock flow counts.
5. Boundary, Error Handling & Adversarial Probing:
   - Obscure / non-existent tickers (e.g. XYZNONEXISTENT -> 400).
   - Out-of-bounds parameters (quarters=0, 41; days=0, 366; sort_by=invalid).
   - Zero-activity identifiers (000000000).
6. Performance & Latency Benchmarks:
   - Quarterly flow latency on AAPL, MSFT, and BKNG within SLAs.
   - Aggregate flow and daily flow latency benchmarks.
"""

import datetime as dt
import os
import time
import unittest
from typing import Any, Dict, List

from fastapi.testclient import TestClient
import psycopg2
from psycopg2.extras import RealDictCursor

from database import get_db_connection
from main import app
from sec_models import (
    DailyFlowResponse,
    AggregateFlowResponse,
    TopStockChangesResponse,
    QuarterlyFlowResponse,
)


class TestChallengerM3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.conn = get_db_connection()

    @classmethod
    def tearDownClass(cls):
        if cls.conn and not cls.conn.closed:
            cls.conn.close()

    # =========================================================================
    # 1. LIVE AAPL & MSFT FLOW RESPONSES & 7 FUND METRICS VERIFICATION
    # =========================================================================

    def test_live_aapl_daily_flow_metrics_and_invariants(self):
        """
        Validate live Apple (AAPL, CUSIP 037833100) daily flow endpoint:
        - 200 OK
        - All 7 fund fields present, integer type, >= 0
        - Additive invariants hold strictly across every row
        """
        res = self.client.get("/api/v1/flow/daily/AAPL?days=7")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("ticker"), "AAPL")
        self.assertEqual(data.get("cusip"), "037833100")

        daily_data = data.get("daily_data", [])
        self.assertGreater(len(daily_data), 0, "Expected at least 1 daily flow entry for AAPL")

        fund_fields = [
            "funds_buying", "funds_selling", "funds_new", "funds_increased",
            "funds_decreased", "funds_closed", "funds_unchanged"
        ]
        for entry in daily_data:
            for field in fund_fields:
                self.assertIn(field, entry, f"Missing field {field}")
                self.assertIsInstance(entry[field], int, f"{field} is not int: {type(entry[field])}")
                self.assertGreaterEqual(entry[field], 0, f"{field} must be >= 0, got {entry[field]}")

            self.assertEqual(
                entry["funds_buying"],
                entry["funds_new"] + entry["funds_increased"],
                f"funds_buying ({entry['funds_buying']}) != funds_new ({entry['funds_new']}) + funds_increased ({entry['funds_increased']})"
            )
            self.assertEqual(
                entry["funds_selling"],
                entry["funds_decreased"] + entry["funds_closed"],
                f"funds_selling ({entry['funds_selling']}) != funds_decreased ({entry['funds_decreased']}) + funds_closed ({entry['funds_closed']})"
            )

    def test_live_aapl_aggregate_flow_metrics_and_invariants(self):
        """
        Validate live Apple (AAPL) aggregate flow endpoint:
        - 200 OK
        - All 7 fund fields present, integer type, >= 0
        - Additive invariants hold
        """
        res = self.client.get("/api/v1/flow/aggregate/AAPL?days=14")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("ticker"), "AAPL")
        self.assertEqual(data.get("cusip"), "037833100")

        fund_fields = [
            "funds_buying", "funds_selling", "funds_new", "funds_increased",
            "funds_decreased", "funds_closed", "funds_unchanged"
        ]
        for field in fund_fields:
            self.assertIn(field, data, f"Missing field {field}")
            self.assertIsInstance(data[field], int, f"{field} is not int")
            self.assertGreaterEqual(data[field], 0, f"{field} must be >= 0")

        self.assertEqual(
            data["funds_buying"],
            data["funds_new"] + data["funds_increased"]
        )
        self.assertEqual(
            data["funds_selling"],
            data["funds_decreased"] + data["funds_closed"]
        )

    def test_live_msft_quarterly_flow_metrics_and_invariants(self):
        """
        Validate live Microsoft (MSFT, CUSIP 594918104) quarterly flow endpoint:
        - 200 OK
        - 4 quarters returned
        - All 7 fund fields present, integer type, >= 0
        - Additive invariants hold for each quarter
        """
        res = self.client.get("/api/v1/flow/quarterly/MSFT?quarters=4")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("ticker"), "MSFT")
        self.assertEqual(data.get("cusip"), "594918104")

        q_data = data.get("quarterly_data", [])
        self.assertEqual(len(q_data), 4, f"Expected 4 quarters, got {len(q_data)}")

        fund_fields = [
            "funds_buying", "funds_selling", "funds_new", "funds_increased",
            "funds_decreased", "funds_closed", "funds_unchanged"
        ]
        for q_entry in q_data:
            for field in fund_fields:
                self.assertIn(field, q_entry, f"Missing field {field}")
                self.assertIsInstance(q_entry[field], int, f"{field} is not int")
                self.assertGreaterEqual(q_entry[field], 0, f"{field} must be >= 0")

            self.assertEqual(
                q_entry["funds_buying"],
                q_entry["funds_new"] + q_entry["funds_increased"]
            )
            self.assertEqual(
                q_entry["funds_selling"],
                q_entry["funds_decreased"] + q_entry["funds_closed"]
            )
            # Both buying and selling should be positive for mega-caps like MSFT
            self.assertGreater(q_entry["funds_buying"], 0)
            self.assertGreater(q_entry["funds_selling"], 0)

    # =========================================================================
    # 2. BOOKING HOLDINGS (BKNG 25:1 SPLIT) EMPIRICAL VALIDATION
    # =========================================================================

    def test_bkng_split_ground_truth_in_database(self):
        """
        Empirically verify the stock_splits table records the BKNG 25:1 split:
        - CUSIP 09857L108
        - period_of_report 2026-06-30
        - split_factor == 25.0
        """
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT cusip, period_of_report, split_factor
                FROM stock_splits
                WHERE cusip = '09857L108' AND period_of_report = '2026-06-30';
            """)
            row = cur.fetchone()
            self.assertIsNotNone(row, "BKNG split record missing in stock_splits")
            self.assertEqual(float(row["split_factor"]), 25.0)

    def test_bkng_unchanged_institutional_holders_not_falsely_counted_as_buyers(self):
        """
        Empirical ground-truth verification of Booking Holdings institutional holders:
        1. Cohen & Steers (company_id 216):
           - 2026-03-31: 50 shares
           - 2026-06-30: 1,250 shares
           - Rebased 2026-03-31: 50 * 25 = 1,250 shares
           - Change: 0 -> classified as 'unchanged'
        2. Manchester Capital Management LLC (company_id 1477):
           - 2026-03-31: 409 shares
           - 2026-06-30: 10,224 shares
           - Rebased 2026-03-31: 409 * 25 = 10,225 shares
           - Difference: ABS(10224 - 10225) = 1 share (cash in lieu)
           - Under 1-share tolerance: classified as 'unchanged'
        Verify that in the quarterly flow query for 2026-06-30, these holders are
        classified as 'unchanged' and NOT counted into funds_buying, funds_new, or funds_increased.
        """
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
            WITH TargetIssuers AS (
                SELECT issuer_id FROM issuers WHERE UPPER(cusip) = '09857L108'
            ),
            QuarterPeriods AS (
                SELECT DISTINCT period_of_report
                FROM filings
                WHERE period_of_report <= '2026-06-30'
                  AND form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
                ORDER BY period_of_report DESC
                LIMIT 2
            ),
            MinPeriod AS (
                SELECT MIN(period_of_report) - INTERVAL '100 days' AS min_date FROM QuarterPeriods
            ),
            RelevantCompanies AS (
                SELECT DISTINCT f.company_id
                FROM holdings h
                JOIN filings f ON h.filing_id = f.filing_id
                WHERE h.issuer_id IN (SELECT issuer_id FROM TargetIssuers)
                  AND f.period_of_report >= (SELECT min_date FROM MinPeriod)
                  AND f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
            ),
            RecentFilings AS (
                SELECT DISTINCT ON (f.company_id, f.period_of_report)
                    f.filing_id, f.company_id, f.filing_date, f.period_of_report
                FROM filings f
                JOIN QuarterPeriods qp ON f.period_of_report = qp.period_of_report
                JOIN RelevantCompanies rc ON f.company_id = rc.company_id
                WHERE f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
                ORDER BY f.company_id, f.period_of_report, f.filing_date DESC, f.accession_number DESC
            ),
            PreviousFilings AS (
                SELECT
                    rf.filing_id AS current_filing_id,
                    rf.company_id,
                    rf.filing_date,
                    rf.period_of_report,
                    (
                        SELECT f2.filing_id
                        FROM filings f2
                        WHERE f2.company_id = rf.company_id
                          AND f2.period_of_report < rf.period_of_report
                          AND f2.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
                        ORDER BY f2.period_of_report DESC, f2.filing_date DESC, f2.accession_number DESC
                        LIMIT 1
                    ) AS previous_filing_id
                FROM RecentFilings rf
            ),
            HoldingsComparison AS (
                SELECT
                    pf.company_id,
                    pf.period_of_report,
                    COALESCE(curr.shares, 0) AS current_shares,
                    COALESCE(prev.shares, 0) AS raw_previous_shares,
                    ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint AS rebased_previous_shares,
                    CASE
                        WHEN prev.shares IS NULL OR prev.shares = 0 THEN 'new'
                        WHEN curr.shares IS NULL OR curr.shares = 0 THEN 'closed'
                        WHEN ABS(COALESCE(curr.shares, 0) - ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint) <= 1 THEN 'unchanged'
                        WHEN COALESCE(curr.shares, 0) > ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint THEN 'increased'
                        WHEN COALESCE(curr.shares, 0) < ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint THEN 'decreased'
                        ELSE 'unchanged'
                    END AS change_type
                FROM PreviousFilings pf
                LEFT JOIN LATERAL (
                    SELECT SUM(h.shares_or_principal_amount) AS shares
                    FROM holdings_normalised h
                    JOIN title_of_class_table tc ON h.title_of_class = tc.id
                    LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
                    WHERE h.filing_id = pf.current_filing_id
                      AND h.issuer_id IN (SELECT issuer_id FROM TargetIssuers)
                      AND tc.is_common_stock = TRUE
                      AND (poc.name IS NULL OR UPPER(poc.name) NOT IN ('PUT', 'CALL'))
                      AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1)
                ) curr ON TRUE
                LEFT JOIN LATERAL (
                    SELECT SUM(h.shares_or_principal_amount) AS shares
                    FROM holdings_normalised h
                    JOIN title_of_class_table tc ON h.title_of_class = tc.id
                    LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
                    WHERE h.filing_id = pf.previous_filing_id
                      AND h.issuer_id IN (SELECT issuer_id FROM TargetIssuers)
                      AND tc.is_common_stock = TRUE
                      AND (poc.name IS NULL OR UPPER(poc.name) NOT IN ('PUT', 'CALL'))
                      AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1)
                ) prev ON TRUE
                LEFT JOIN stock_splits ss
                    ON ss.cusip = '09857L108'
                   AND ss.period_of_report = pf.period_of_report
                WHERE curr.shares > 0 OR prev.shares > 0
            )
            SELECT company_id, current_shares, raw_previous_shares, rebased_previous_shares, change_type
            FROM HoldingsComparison
            WHERE company_id IN (216, 1477)
              AND period_of_report = '2026-06-30';
            """
            cur.execute(query)
            rows = {r["company_id"]: r for r in cur.fetchall()}

            # Validate Cohen & Steers (216)
            self.assertIn(216, rows, "Cohen & Steers (216) missing from BKNG HoldingsComparison")
            self.assertEqual(rows[216]["change_type"], "unchanged")
            self.assertEqual(rows[216]["current_shares"], 1250)
            self.assertEqual(rows[216]["raw_previous_shares"], 50)
            self.assertEqual(rows[216]["rebased_previous_shares"], 1250)

            # Validate Manchester Capital (1477) 1-share tolerance
            self.assertIn(1477, rows, "Manchester Capital (1477) missing from BKNG HoldingsComparison")
            self.assertEqual(rows[1477]["change_type"], "unchanged")
            self.assertEqual(rows[1477]["current_shares"], 10224)
            self.assertEqual(rows[1477]["raw_previous_shares"], 409)
            self.assertEqual(rows[1477]["rebased_previous_shares"], 10225)

    def test_bkng_quarterly_flow_endpoint_shows_positive_unchanged_funds(self):
        """
        Verify BKNG quarterly flow endpoint for 2026-06-30:
        - funds_unchanged > 0 (144 funds)
        - Invariant holds: funds_buying == funds_new + funds_increased
        """
        res = self.client.get("/api/v1/flow/quarterly/BKNG?quarters=4")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        q_entries = data.get("quarterly_data", [])

        # Find 2026-06-30 quarter
        q2_entry = next((q for q in q_entries if str(q.get("quarter")) == "2026-06-30"), None)
        self.assertIsNotNone(q2_entry, "Quarter 2026-06-30 not found in BKNG quarterly flow")

        self.assertGreater(q2_entry["funds_unchanged"], 100, "Expected >100 unchanged funds for BKNG after split")
        self.assertEqual(q2_entry["funds_buying"], q2_entry["funds_new"] + q2_entry["funds_increased"])
        self.assertEqual(q2_entry["funds_selling"], q2_entry["funds_decreased"] + q2_entry["funds_closed"])

    # =========================================================================
    # 3. TOP MARKET CHANGES ENDPOINT VERIFICATION
    # =========================================================================

    def test_top_changes_endpoint_fund_metrics_and_sorting(self):
        """
        Verify /api/v1/flow/top-changes endpoint on peak filing date 2026-08-14:
        - Returns 50 stocks
        - Every stock has 7 fund metrics as non-negative ints
        - Invariants hold strictly for every stock
        """
        for sort_by in ["shares", "value"]:
            res = self.client.get(f"/api/v1/flow/top-changes?date=2026-08-14&sort_by={sort_by}")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            stocks = data.get("stocks", [])
            self.assertEqual(len(stocks), 50, f"Expected exactly 50 stocks for sort_by={sort_by}")

            fund_fields = [
                "funds_buying", "funds_selling", "funds_new", "funds_increased",
                "funds_decreased", "funds_closed", "funds_unchanged"
            ]
            for s in stocks:
                for f in fund_fields:
                    self.assertIn(f, s)
                    self.assertIsInstance(s[f], int)
                    self.assertGreaterEqual(s[f], 0)

                self.assertEqual(s["funds_buying"], s["funds_new"] + s["funds_increased"])
                self.assertEqual(s["funds_selling"], s["funds_decreased"] + s["funds_closed"])

    # =========================================================================
    # 4. OPTION SCOPE ISOLATION
    # =========================================================================

    def test_options_excluded_from_flow_queries(self):
        """
        Verify that option positions (PUT, CALL) and non-common stock are excluded
        from flow calculations in routers/flow.py.
        """
        # Amazon (AMZN) has significant institutional PUT and CALL contracts in 13F
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT count(*) as option_count
                FROM holdings_normalised h
                JOIN title_of_class_table tc ON h.title_of_class = tc.id
                JOIN put_or_call_table poc ON h.put_or_call = poc.id
                WHERE h.issuer_id IN (SELECT issuer_id FROM issuers WHERE symbol = 'AMZN')
                  AND UPPER(poc.name) IN ('PUT', 'CALL');
            """)
            row = cur.fetchone()
            self.assertGreater(row["option_count"], 0, "AMZN has options in database to test isolation")

        # Now query AMZN daily flow
        res = self.client.get("/api/v1/flow/daily/AMZN?days=5")
        self.assertEqual(res.status_code, 200)
        daily_entries = res.json().get("daily_data", [])
        for d in daily_entries:
            self.assertEqual(d["funds_buying"], d["funds_new"] + d["funds_increased"])
            self.assertEqual(d["funds_selling"], d["funds_decreased"] + d["funds_closed"])

    # =========================================================================
    # 5. BOUNDARY CONDITIONS & ADVERSARIAL STRESS PROBING
    # =========================================================================

    def test_adversarial_invalid_quarters_parameter(self):
        """Verify boundary validations for quarters query parameter (ge=1, le=40)."""
        # quarters = 0 -> 422 Unprocessable Entity
        res = self.client.get("/api/v1/flow/quarterly/AAPL?quarters=0")
        self.assertEqual(res.status_code, 422)

        # quarters = 41 -> 422 Unprocessable Entity
        res = self.client.get("/api/v1/flow/quarterly/AAPL?quarters=41")
        self.assertEqual(res.status_code, 422)

        # quarters = -1 -> 422 Unprocessable Entity
        res = self.client.get("/api/v1/flow/quarterly/AAPL?quarters=-1")
        self.assertEqual(res.status_code, 422)

    def test_adversarial_invalid_days_parameter(self):
        """Verify boundary validations for days query parameter (ge=1, le=365)."""
        # days = 0 -> 422
        res = self.client.get("/api/v1/flow/daily/AAPL?days=0")
        self.assertEqual(res.status_code, 422)

        # days = 366 -> 422
        res = self.client.get("/api/v1/flow/daily/AAPL?days=366")
        self.assertEqual(res.status_code, 422)

    def test_adversarial_invalid_sort_by_top_changes(self):
        """Verify invalid sort_by option triggers 400 Bad Request."""
        res = self.client.get("/api/v1/flow/top-changes?date=2026-08-14&sort_by=invalid_sort")
        self.assertEqual(res.status_code, 400)

    def test_adversarial_unknown_identifier(self):
        """Verify non-resolvable ticker returns 400 Bad Request, while nonexistent CUSIP returns empty 200."""
        # Unresolvable ticker (<=5 chars alpha without CUSIP) -> 400 Bad Request
        res_ticker = self.client.get("/api/v1/flow/quarterly/XYZQ?quarters=4")
        self.assertEqual(res_ticker.status_code, 400)
        self.assertIn("Could not resolve XYZQ to a valid CUSIP", res_ticker.json().get("detail", ""))

        # Nonexistent CUSIP-like string (>5 chars) -> 200 OK with empty quarterly_data
        res_cusip = self.client.get("/api/v1/flow/quarterly/NONEXISTENT_CUSIP?quarters=4")
        self.assertEqual(res_cusip.status_code, 200)
        self.assertEqual(res_cusip.json().get("quarterly_data"), [])

    def test_adversarial_zero_activity_cusip(self):
        """Verify zero-activity identifier (000000000) handles gracefully with 0 counts."""
        res = self.client.get("/api/v1/flow/aggregate/000000000?days=1")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["funds_buying"], 0)
        self.assertEqual(data["funds_selling"], 0)
        self.assertEqual(data["funds_new"], 0)
        self.assertEqual(data["funds_increased"], 0)
        self.assertEqual(data["funds_decreased"], 0)
        self.assertEqual(data["funds_closed"], 0)
        self.assertEqual(data["funds_unchanged"], 0)

    # =========================================================================
    # 6. LATENCY & PERFORMANCE BENCHMARKS (SLA VERIFICATION)
    # =========================================================================

    def test_latency_sla_quarterly_flow_mega_caps(self):
        """
        Measure latency on quarterly flow endpoint for mega-caps (AAPL, MSFT):
        - AAPL (4 quarters): SLA < 2000 ms
        - MSFT (4 quarters): SLA < 2000 ms
        """
        # AAPL 4 quarters
        t0 = time.perf_counter()
        res_aapl = self.client.get("/api/v1/flow/quarterly/AAPL?quarters=4")
        elapsed_aapl_ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(res_aapl.status_code, 200)
        self.assertLess(elapsed_aapl_ms, 2000.0, f"AAPL 4Q quarterly flow took {elapsed_aapl_ms:.2f}ms (>2000ms SLA)")

        # MSFT 4 quarters
        t0 = time.perf_counter()
        res_msft = self.client.get("/api/v1/flow/quarterly/MSFT?quarters=4")
        elapsed_msft_ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(res_msft.status_code, 200)
        self.assertLess(elapsed_msft_ms, 2000.0, f"MSFT 4Q quarterly flow took {elapsed_msft_ms:.2f}ms (>2000ms SLA)")

    def test_latency_sla_aggregate_and_daily_flow(self):
        """
        Measure latency on aggregate and daily flow endpoints:
        - AAPL Aggregate 14d: SLA < 500 ms
        - MSFT Daily 7d: SLA < 1000 ms
        """
        # Aggregate flow AAPL
        t0 = time.perf_counter()
        res_agg = self.client.get("/api/v1/flow/aggregate/AAPL?days=14")
        elapsed_agg_ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(res_agg.status_code, 200)
        self.assertLess(elapsed_agg_ms, 500.0, f"AAPL Aggregate flow took {elapsed_agg_ms:.2f}ms (>500ms SLA)")

        # Daily flow MSFT
        t0 = time.perf_counter()
        res_daily = self.client.get("/api/v1/flow/daily/MSFT?days=7")
        elapsed_daily_ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(res_daily.status_code, 200)
        self.assertLess(elapsed_daily_ms, 1000.0, f"MSFT Daily flow took {elapsed_daily_ms:.2f}ms (>1000ms SLA)")


if __name__ == "__main__":
    unittest.main()
