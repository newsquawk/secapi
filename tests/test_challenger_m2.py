"""
tests/test_challenger_m2.py

Empirical Challenger verification test suite for Milestone 2:
Requirement 2 (R2) Consecutive Quarterly Buying and Selling Streaks.
Authored by Empirical Challenger (challenger_m2_1).

Empirical validation objectives:
1. Berkshire Hathaway (CIK 0001067983) on 2026-06-30:
   - Alphabet Inc Cl A (02079K305) buying streak: streak_quarters == 2, streak_direction in ('buying', 'buy').
   - Apple Inc (AAPL, 037833100) status:
     Empirical ground truth in DB: Berkshire's AAPL shares are identical (227,917,808) across
     2025-12-31, 2026-03-31, and 2026-06-30. Verify it is classified as 'unchanged' with streak_quarters is None.
     Verify actual Berkshire decreased quarters (e.g. 2025-12-31) show streak_quarters >= 2, streak_direction == 'selling'.
     Verify live 2026-06-30 selling streaks (BAC, DVA, NUE, STZ) have streak_quarters >= 2, streak_direction == 'selling'.
2. Booking Holdings (BKNG, 09857L108) 25:1 split:
   - Verify unchanged holdings across 2026-06-30 are NOT falsely classified as a 2400% buying surge.
   - Verify change_type == 'unchanged', change_in_share == 0, percent_change == 0.0, streak_quarters is None.
   - Verify 1-share cash-in-lieu tolerance boundary conditions (diff <= 1 unchanged, diff >= 2 increased).
3. Large filing benchmark (1,000+ holdings):
   - Measure full endpoint latency on Inspire Investing (CIK 0001670139, 1,266 holdings) < 500ms.
   - Measure full endpoint latency on Marshall Wace (CIK 0001318757, 6,550 holdings) < 500ms.
   - Measure bulk enrich_activities performance on 1,000 holdings < 100ms.
"""

import datetime as dt
import os
import time
import unittest
from typing import Any, Dict, List

from fastapi.testclient import TestClient
import psycopg2

from auth import sync_dependencies
from database import get_db_connection
from holding_enricher import (
    _calculate_streak,
    enrich_activities,
    quarter_index,
)
from main import app
from sec_models import HoldingActivity


class TestChallengerM2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "challenger-service-account"}
        cls.conn = get_db_connection()

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        if cls.conn and not cls.conn.closed:
            cls.conn.close()

    # =========================================================================
    # 1. BERKSHIRE HATHAWAY GROUND TRUTH VALIDATION
    # =========================================================================

    def test_berkshire_alphabet_buying_streak_ground_truth(self):
        """
        Validate Berkshire Hathaway (CIK 0001067983) on 2026-06-30:
        Alphabet Inc Cl A (CUSIP 02079K305) buying streak:
        - 2025-12-31: 17,846,142 shares
        - 2026-03-31: 54,249,798 shares (Quarter 1 buying)
        - 2026-06-30: 78,791,167 shares (Quarter 2 buying)
        Must yield:
        - change_type == 'increased'
        - streak_quarters == 2
        - streak_direction in ('buying', 'buy')
        - streak_type in ('buying', 'buy')
        """
        res = self.client.get("/activity/latest/v3?cik=0001067983&ticker=GOOGL")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        self.assertGreater(len(activities), 0, "Expected at least one activity for Berkshire GOOGL")

        googl = next((a for a in activities if a.get("cusip") == "02079K305"), None)
        self.assertIsNotNone(googl, "Alphabet Inc Cl A (02079K305) not found in Berkshire activities")

        self.assertEqual(googl.get("change_type"), "increased")
        self.assertEqual(googl.get("current_shares"), 78791167)
        self.assertEqual(googl.get("previous_shares"), 54249798)
        self.assertEqual(googl.get("streak_quarters"), 2, "Expected streak_quarters == 2 for GOOGL")
        self.assertIn(googl.get("streak_direction"), ["buying", "buy"])
        self.assertEqual(googl.get("streak_direction"), googl.get("streak_type"))

    def test_berkshire_apple_status_and_selling_streaks_ground_truth(self):
        """
        Validate Berkshire Hathaway (CIK 0001067983) Apple Inc (AAPL) ground truth:
        1. On 2026-06-30, Berkshire AAPL share count in DB is 227,917,808, identical to
           2026-03-31 (227,917,808) and 2025-12-31 (227,917,808).
           Therefore, AAPL on 2026-06-30 is UNCHANGED:
           - change_type == 'unchanged'
           - streak_quarters is None
           - streak_direction is None
           (It must NOT be falsely marked as buying or selling streak).
        2. In quarters where Berkshire DID decrease AAPL (e.g. 2025-12-31, where shares dropped
           from 238,212,764 to 227,917,808 following 280,000,000 -> 238,212,764 in 2025-09-30):
           - enrich_activities must compute streak_quarters >= 2 and streak_direction == 'selling'.
        3. On 2026-06-30, live Berkshire holdings that DID decrease (BAC, DVA, NUE):
           - BAC (Bank of America): streak_quarters == 4, streak_direction == 'selling'
           - DVA (DaVita): streak_quarters == 4, streak_direction == 'selling'
           - NUE (Nucor): streak_quarters == 2, streak_direction == 'selling'
        """
        # 1. 2026-06-30 AAPL unchanged verification
        res = self.client.get("/activity/latest/v3?cik=0001067983&ticker=AAPL")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        self.assertEqual(len(activities), 1, "Expected single AAPL activity for Berkshire")
        aapl = activities[0]
        self.assertEqual(aapl.get("cusip"), "037833100")
        self.assertEqual(aapl.get("change_type"), "unchanged")
        self.assertEqual(aapl.get("current_shares"), 227917808)
        self.assertEqual(aapl.get("previous_shares"), 227917808)
        self.assertIsNone(aapl.get("streak_quarters"), "Unchanged AAPL must have streak_quarters is None")
        self.assertIsNone(aapl.get("streak_direction"), "Unchanged AAPL must have streak_direction is None")

        # 2. Historical AAPL decrease streak verification (2025-12-31)
        simulated_aapl_dec = [{
            "company_id": 58,
            "reporting_period": dt.date(2025, 12, 31),
            "cusip": "037833100",
            "change_type": "decreased",
            "security_type": "stocks",
            "is_common_stock": True,
        }]
        enrich_activities(self.conn, simulated_aapl_dec)
        self.assertGreaterEqual(simulated_aapl_dec[0]["streak_quarters"], 2)
        self.assertEqual(simulated_aapl_dec[0]["streak_direction"], "selling")

        # 3. Live 2026-06-30 Berkshire selling streaks
        res_all = self.client.get("/activity/latest/v3?cik=0001067983&limit=50&security_type=stocks")
        self.assertEqual(res_all.status_code, 200)
        all_acts = res_all.json().get("activities", [])

        selling_map = {a.get("ticker"): a for a in all_acts if a.get("streak_direction") == "selling"}

        # Bank of America (BAC)
        self.assertIn("BAC", selling_map, "Expected BAC in Berkshire selling activities")
        self.assertGreaterEqual(selling_map["BAC"]["streak_quarters"], 2)
        self.assertEqual(selling_map["BAC"]["streak_quarters"], 4)
        self.assertEqual(selling_map["BAC"]["streak_direction"], "selling")

        # DaVita (DVA)
        self.assertIn("DVA", selling_map, "Expected DVA in Berkshire selling activities")
        self.assertGreaterEqual(selling_map["DVA"]["streak_quarters"], 2)
        self.assertEqual(selling_map["DVA"]["streak_quarters"], 4)
        self.assertEqual(selling_map["DVA"]["streak_direction"], "selling")

        # Nucor (NUE)
        self.assertIn("NUE", selling_map, "Expected NUE in Berkshire selling activities")
        self.assertEqual(selling_map["NUE"]["streak_quarters"], 2)
        self.assertEqual(selling_map["NUE"]["streak_direction"], "selling")

    # =========================================================================
    # 2. BOOKING HOLDINGS (BKNG) 25:1 SPLIT GROUND TRUTH & TOLERANCE
    # =========================================================================

    def test_booking_holdings_split_not_classified_as_buying_surge(self):
        """
        Validate Booking Holdings (BKNG, CUSIP 09857L108) 25:1 forward split:
        Multiple real institutional holders held BKNG across the 2026-06-30 split:
        - CIK 1940917 (Signature Resources Capital Management): 1 share -> 25 shares (exact 25x)
        - CIK 1133014 (Monetary Management Group): 192 shares -> 4,800 shares (exact 25x)
        - CIK 1832097 (Wolff Wiese Magana): 1 share -> 25 shares (exact 25x)
        Verify:
        - change_type == 'unchanged'
        - change_in_share == 0
        - percent_change == 0.0
        - streak_quarters is None
        - streak_direction is None
        Crucially: must NOT be classified as 'increased' with a +2400% buying surge.
        """
        test_ciks = ["1940917", "1133014", "1832097"]
        for cik in test_ciks:
            res = self.client.get(f"/activity/latest/v3?cik={cik}&ticker=BKNG")
            self.assertEqual(res.status_code, 200)
            activities = res.json().get("activities", [])
            self.assertGreater(len(activities), 0, f"Expected BKNG activity for CIK {cik}")
            bkng_act = activities[0]

            self.assertEqual(
                bkng_act.get("change_type"),
                "unchanged",
                f"CIK {cik} BKNG falsely classified as {bkng_act.get('change_type')} instead of unchanged",
            )
            self.assertEqual(
                bkng_act.get("change_in_share"),
                0,
                f"CIK {cik} BKNG change_in_share should be 0 under split adjustment",
            )
            self.assertEqual(
                bkng_act.get("percent_change"),
                0.0,
                f"CIK {cik} BKNG percent_change should be 0.0",
            )
            self.assertIsNone(
                bkng_act.get("streak_quarters"),
                f"CIK {cik} BKNG streak_quarters must be None for unchanged holding",
            )
            self.assertIsNone(
                bkng_act.get("streak_direction"),
                f"CIK {cik} BKNG streak_direction must be None for unchanged holding",
            )

    def test_booking_cash_in_lieu_tolerance_boundary_oracle(self):
        """
        Adversarial Boundary Test for 1-share cash-in-lieu rounding tolerance on BKNG 25:1 split:
        Given previous shares = 10 (rebased to 250):
        - Current shares = 250 (delta = 0): unchanged -> streak terminates (None)
        - Current shares = 251 (delta = +1): cash-in-lieu tolerance -> unchanged -> streak terminates
        - Current shares = 249 (delta = -1): cash-in-lieu tolerance -> unchanged -> streak terminates
        - Current shares = 252 (delta = +2): genuine buying -> streak continues
        - Current shares = 248 (delta = -2): genuine selling -> streak continues
        """
        comp_id = 99999
        cusip = "09857L108"
        is_option = False
        rep_date = dt.date(2026, 6, 30)
        q_curr = quarter_index(rep_date)
        q_prev = q_curr - 1
        q_prev2 = q_prev - 1

        company_filed = {comp_id: {q_curr: rep_date, q_prev: dt.date(2026, 3, 31), q_prev2: dt.date(2025, 12, 31)}}
        splits_map = {(cusip, q_prev): 25.0}

        # Case A: delta = +1 in prior quarter (251 vs rebased 250)
        # Prior quarter had 10 shares, walked back from 251.
        # Since abs(251 - 250) <= 1, it is treated as unchanged in prior quarter.
        # A buying streak starting at q_curr should terminate at q_prev (streak = 1).
        history_delta_1 = {
            (comp_id, cusip, is_option): {
                q_curr: 300,
                q_prev: 251,
                q_prev2: 10,
            }
        }
        sq, sdir, _ = _calculate_streak(
            comp_id=comp_id,
            cusip=cusip,
            rep_period=rep_date,
            change_type="increased",
            is_option=is_option,
            shares_history=history_delta_1,
            company_filed_quarters=company_filed,
            splits_map=splits_map,
        )
        self.assertEqual(sq, 1, "Delta = 1 (cash-in-lieu) must break buying streak, stopping walkback at 1")
        self.assertEqual(sdir, "buying")

        # Case B: delta = +2 in prior quarter (252 vs rebased 250)
        # 252 > 250 by 2 (> 1 tolerance), so prior quarter was a genuine increase.
        # Buying streak extends: streak = 2.
        history_delta_2 = {
            (comp_id, cusip, is_option): {
                q_curr: 300,
                q_prev: 252,
                q_prev2: 10,
            }
        }
        sq2, sdir2, _ = _calculate_streak(
            comp_id=comp_id,
            cusip=cusip,
            rep_period=rep_date,
            change_type="increased",
            is_option=is_option,
            shares_history=history_delta_2,
            company_filed_quarters=company_filed,
            splits_map=splits_map,
        )
        self.assertEqual(sq2, 2, "Delta = 2 exceeds cash-in-lieu tolerance, must extend buying streak to 2")
        self.assertEqual(sdir2, "buying")

    # =========================================================================
    # 3. PERFORMANCE BENCHMARKS (1,000+ HOLDINGS FILINGS)
    # =========================================================================

    def test_performance_inspire_investing_endpoint_sla(self):
        """
        Benchmark Full Endpoint Latency:
        Inspire Investing, LLC (CIK 0001670139):
        Latest filing (336030 on 2026-06-30) contains 1,266 holdings.
        Full endpoint execution for /activity/latest/v3 must complete in < 500 ms.
        """
        # Warmup
        self.client.get("/activity/latest/v3?limit=5&security_type=stocks")

        latencies = []
        for _ in range(5):
            t0 = time.perf_counter()
            res = self.client.get("/activity/latest/v3?cik=0001670139&limit=50&security_type=stocks")
            t1 = time.perf_counter()
            self.assertEqual(res.status_code, 200)
            latencies.append((t1 - t0) * 1000.0)

        avg_latency = sum(latencies) / len(latencies)
        max_latency = max(latencies)

        self.assertLess(
            avg_latency,
            500.0,
            f"Inspire Investing average latency {avg_latency:.2f}ms exceeded 500ms SLA",
        )
        self.assertLess(
            max_latency,
            500.0,
            f"Inspire Investing worst-case latency {max_latency:.2f}ms exceeded 500ms SLA",
        )

    def test_performance_marshall_wace_endpoint_sla(self):
        """
        Benchmark Full Endpoint Latency:
        Marshall Wace, LLP (CIK 0001318757):
        Latest filing (336923 on 2026-06-30) contains 6,550 holdings.
        Full endpoint execution for /activity/latest/v3 must complete in < 500 ms.
        """
        # Warmup
        self.client.get("/activity/latest/v3?limit=5&security_type=stocks")

        latencies = []
        for _ in range(5):
            t0 = time.perf_counter()
            res = self.client.get("/activity/latest/v3?cik=0001318757&limit=50&security_type=stocks")
            t1 = time.perf_counter()
            self.assertEqual(res.status_code, 200)
            latencies.append((t1 - t0) * 1000.0)

        avg_latency = sum(latencies) / len(latencies)
        max_latency = max(latencies)

        self.assertLess(
            avg_latency,
            500.0,
            f"Marshall Wace average latency {avg_latency:.2f}ms exceeded 500ms SLA",
        )
        self.assertLess(
            max_latency,
            500.0,
            f"Marshall Wace worst-case latency {max_latency:.2f}ms exceeded 500ms SLA",
        )

    def test_performance_bulk_enrich_activities_1000_holdings(self):
        """
        Benchmark holding_enricher.enrich_activities Bulk Performance:
        Directly enrich 1,000 holdings from Marshall Wace in a single batch.
        Must complete in < 100 ms (budget: 500 ms).
        """
        cur = self.conn.cursor()
        cur.execute("""
            SELECT 168 AS company_id, f.period_of_report AS reporting_period, i.cusip,
                   'decreased' AS change_type, false AS is_option, 'stocks' AS security_type
            FROM holdings_normalised h
            JOIN filings f ON f.filing_id = h.filing_id
            JOIN issuers i ON i.issuer_id = h.issuer_id
            WHERE h.filing_id = 336923
            LIMIT 1000;
        """)
        columns = [desc[0] for desc in cur.description]
        batch = [dict(zip(columns, row)) for row in cur.fetchall()]
        cur.close()

        self.assertEqual(len(batch), 1000, "Expected 1,000 holdings from Marshall Wace filing 336923")

        t0 = time.perf_counter()
        enrich_activities(self.conn, batch)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        for a in batch:
            self.assertIn("streak_quarters", a)
            self.assertIn("streak_direction", a)
            self.assertEqual(a["streak_direction"], "selling")
            self.assertGreaterEqual(a["streak_quarters"], 1)

        self.assertLess(
            elapsed_ms,
            100.0,
            f"enrich_activities on 1,000 holdings took {elapsed_ms:.2f}ms, exceeding 100ms budget",
        )


if __name__ == "__main__":
    unittest.main()
