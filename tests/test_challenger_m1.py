"""
tests/test_challenger_m1.py

Adversarial Stress Harness & Empirical Verification for Milestone 1:
R1 Position Entry Type ('first_ever' vs 're_entry' vs None).

Executed by: challenger_m1_1 (Empirical Challenger)
"""

import datetime as dt
import os
import time
import unittest
from typing import Any, Dict, List
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import psycopg2

from auth import sync_dependencies
from database import get_db_connection
from holding_enricher import enrich_activities
from main import app
from sec_models import HoldingActivity


class TestChallengerM1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "test-service-account"}
        cls.conn = get_db_connection()

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        if cls.conn and not cls.conn.closed:
            cls.conn.close()

    # =========================================================================
    # 1. GROUND TRUTH DATABASE VALIDATION
    # =========================================================================

    def test_ground_truth_berkshire_dr_horton_re_entry(self):
        """
        Ground Truth: Berkshire Hathaway (company_id=58 / CIK 0001067983)
        on 2026-06-30 for D R Horton (CUSIP 23331A109) MUST be 're_entry'.
        """
        items = [
            {
                "company_id": 58,
                "cik": "0001067983",
                "period_of_report": dt.date(2026, 6, 30),
                "cusip": "23331A109",
                "change_type": "new",
            }
        ]
        enrich_activities(self.conn, items)
        self.assertEqual(items[0]["position_entry_type"], "re_entry")
        self.assertEqual(items[0]["entry_type"], "re_entry")

    def test_ground_truth_inspire_investing_affirm_first_ever(self):
        """
        Ground Truth: Inspire Investing (company_id=8333 / CIK 0001670139)
        on 2026-06-30 for Affirm Holdings (CUSIP 00827B106) MUST be 'first_ever'.
        """
        items = [
            {
                "company_id": 8333,
                "cik": "0001670139",
                "period_of_report": dt.date(2026, 6, 30),
                "cusip": "00827B106",
                "change_type": "new",
            }
        ]
        enrich_activities(self.conn, items)
        self.assertEqual(items[0]["position_entry_type"], "first_ever")
        self.assertEqual(items[0]["entry_type"], "first_ever")

    def test_ground_truth_non_new_positions_return_none(self):
        """
        Ground truth invariant: Non-new positions ('increased', 'decreased',
        'unchanged', 'closed') MUST return None (null) for position_entry_type and entry_type.
        """
        for ctype in ["increased", "decreased", "unchanged", "closed", "INCREASED", " Decreased "]:
            items = [
                {
                    "company_id": 58,
                    "period_of_report": dt.date(2026, 6, 30),
                    "cusip": "23331A109",
                    "change_type": ctype,
                }
            ]
            enrich_activities(self.conn, items)
            self.assertIsNone(
                items[0]["position_entry_type"],
                f"Failed invariant: position_entry_type should be None for change_type='{ctype}'"
            )
            self.assertIsNone(
                items[0]["entry_type"],
                f"Failed invariant: entry_type should be None for change_type='{ctype}'"
            )

    # =========================================================================
    # 2. ADVERSARIAL STRESS CASES: Cross-Quarter, Mixed-Batch, Same-Period
    # =========================================================================

    def test_mixed_reporting_periods_in_single_batch(self):
        """
        Adversarial Scenario: A single batch contains Berkshire holding DHI at two
        different historical periods:
        1. 2023-06-30 (initial purchase -> first_ever, no prior filings exist for DHI)
        2. 2026-06-30 (re-entry -> re_entry, 2023 filings exist)
        Enricher must not cross-contaminate classifications due to max_period optimization.
        """
        items = [
            {
                "company_id": 58,
                "period_of_report": dt.date(2023, 6, 30),
                "cusip": "23331A109",
                "change_type": "new",
            },
            {
                "company_id": 58,
                "period_of_report": dt.date(2026, 6, 30),
                "cusip": "23331A109",
                "change_type": "new",
            },
        ]
        enrich_activities(self.conn, items)
        self.assertEqual(
            items[0]["position_entry_type"],
            "first_ever",
            "Berkshire 2023-06-30 DHI was the initial entry, must be first_ever"
        )
        self.assertEqual(
            items[1]["position_entry_type"],
            "re_entry",
            "Berkshire 2026-06-30 DHI followed 2023 entry, must be re_entry"
        )

    def test_same_period_holdings_do_not_trigger_reentry(self):
        """
        Adversarial Scenario: If a fund held a security only in the current period
        (e.g. multiple holdings or amendments in 2026-06-30), it must not evaluate
        as re_entry. Prior date check must be strictly prior (< period_of_report).
        """
        items = [
            {
                "company_id": 8333,
                "period_of_report": dt.date(2026, 6, 30),
                "cusip": "00827B106",  # Affirm in 2026-06-30
                "change_type": "new",
            }
        ]
        enrich_activities(self.conn, items)
        self.assertEqual(items[0]["position_entry_type"], "first_ever")

    def test_adversarial_input_variants(self):
        """
        Adversarial Scenario: Dirty data formats
        - lower case CUSIP
        - whitespace padded CUSIP
        - string date representation '2026-06-30'
        - datetime object dt.datetime(2026, 6, 30, 10, 30)
        - reporting_period instead of period_of_report
        - cik instead of company_id
        """
        items = [
            {
                "cik": "0001067983",
                "reporting_period": "2026-06-30",
                "cusip": "  23331a109  ",  # lowercase + spaces
                "change_type": " NEW ",     # uppercase + spaces
            },
            {
                "cik": "1670139",          # unpadded CIK
                "period_of_report": dt.datetime(2026, 6, 30, 0, 0),
                "cusip": "00827b106",
                "change_type": "new",
            },
        ]
        enrich_activities(self.conn, items)
        self.assertEqual(items[0]["position_entry_type"], "re_entry")
        self.assertEqual(items[0]["entry_type"], "re_entry")
        self.assertEqual(items[1]["position_entry_type"], "first_ever")
        self.assertEqual(items[1]["entry_type"], "first_ever")

    def test_missing_or_corrupted_metadata_graceful_handling(self):
        """
        Adversarial Scenario: Missing company_id, missing CIK, missing period, or missing CUSIP.
        Should default gracefully to 'first_ever' without throwing an unhandled exception.
        """
        items = [
            {"change_type": "new", "cusip": None},
            {"change_type": "new", "cusip": "23331A109", "period_of_report": None},
            {"change_type": "new", "cusip": "23331A109", "company_id": None, "cik": None},
            {"change_type": None, "cusip": "23331A109"},
        ]
        enrich_activities(self.conn, items)
        self.assertEqual(items[0]["position_entry_type"], "first_ever")
        self.assertEqual(items[1]["position_entry_type"], "first_ever")
        self.assertEqual(items[2]["position_entry_type"], "first_ever")
        self.assertIsNone(items[3]["position_entry_type"])

    def test_db_exception_fallback(self):
        """
        Adversarial Scenario: Database cursor throws an error during execution.
        enrich_activities must catch it, log, and assign graceful fallbacks.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.execute.side_effect = psycopg2.OperationalError("Simulated DB connection failure")

        items = [
            {"company_id": 58, "period_of_report": "2026-06-30", "cusip": "23331A109", "change_type": "new"},
            {"company_id": 58, "period_of_report": "2026-06-30", "cusip": "037833100", "change_type": "increased"},
        ]
        res = enrich_activities(mock_conn, items)
        self.assertEqual(res[0]["position_entry_type"], "first_ever")
        self.assertIsNone(res[1]["position_entry_type"])

    # =========================================================================
    # 3. SCALE & PERFORMANCE BENCHMARK (< 500ms, ZERO N+1)
    # =========================================================================

    def test_performance_1000_holdings_under_500ms(self):
        """
        Performance Requirement: enrich_activities on 1,000+ holdings must complete
        in < 500 ms and execute strictly zero N+1 queries.
        """
        # Fetch 1,000 real candidate holdings from filing_id 18072 (company 309)
        cur = self.conn.cursor()
        cur.execute("""
            SELECT UPPER(i.cusip) as cusip, 'new' as change_type, 309 as company_id, '2020-09-30' as period_of_report
            FROM holdings h
            JOIN issuers i ON h.issuer_id = i.issuer_id
            WHERE h.filing_id = 18072
            LIMIT 1000;
        """)
        raw_holdings = [
            {
                "cusip": row[0],
                "change_type": row[1],
                "company_id": row[2],
                "period_of_report": row[3],
            }
            for row in cur.fetchall()
        ]
        cur.close()
        self.assertGreaterEqual(len(raw_holdings), 1000)

        # Benchmark run
        start_time = time.perf_counter()
        enriched = enrich_activities(self.conn, raw_holdings)
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        print(f"\n[BENCHMARK] enrich_activities on {len(enriched)} holdings: {elapsed_ms:.2f} ms (budget: 500 ms)")
        self.assertLess(
            elapsed_ms,
            500.0,
            f"Latency exceeded budget: {elapsed_ms:.2f} ms >= 500 ms"
        )
        # All items should have valid entry_type
        for item in enriched:
            self.assertIn(item["position_entry_type"], ["first_ever", "re_entry"])
            self.assertEqual(item["position_entry_type"], item["entry_type"])

    # =========================================================================
    # 4. ENDPOINT CONTRACT EMPIRICAL VERIFICATION
    # =========================================================================

    def test_endpoint_activity_latest_v3_berkshire(self):
        """
        Verify GET /activity/latest/v3 for Berkshire DHI returns re_entry.
        """
        res = self.client.get("/activity/latest/v3?cik=0001067983&ticker=DHI")
        self.assertEqual(res.status_code, 200)
        acts = res.json().get("activities", [])
        self.assertGreaterEqual(len(acts), 1)
        dhi_new = [a for a in acts if a.get("change_type") == "new" and "23331A109" in a.get("cusip", "")]
        self.assertTrue(len(dhi_new) >= 1)
        for act in dhi_new:
            self.assertEqual(act["position_entry_type"], "re_entry")
            self.assertEqual(act["entry_type"], "re_entry")

    def test_endpoint_activity_latest_v3_inspire(self):
        """
        Verify GET /activity/latest/v3 for Inspire AFRM returns first_ever.
        """
        res = self.client.get("/activity/latest/v3?cik=0001670139&ticker=AFRM")
        self.assertEqual(res.status_code, 200)
        acts = res.json().get("activities", [])
        self.assertGreaterEqual(len(acts), 1)
        afrm_new = [a for a in acts if a.get("change_type") == "new" and "00827B106" in a.get("cusip", "")]
        self.assertTrue(len(afrm_new) >= 1)
        for act in afrm_new:
            self.assertEqual(act["position_entry_type"], "first_ever")
            self.assertEqual(act["entry_type"], "first_ever")

    def test_endpoint_changes_feed(self):
        """
        Verify GET /changes exposes position_entry_type and entry_type on activities.
        """
        res = self.client.get("/changes?limit=2")
        self.assertEqual(res.status_code, 200)
        items = res.json().get("items", [])
        self.assertGreaterEqual(len(items), 1)
        for envelope in items:
            for act in envelope.get("activities", []):
                self.assertIn("position_entry_type", act)
                self.assertIn("entry_type", act)
                if act.get("change_type") == "new":
                    self.assertIn(act.get("position_entry_type"), ["first_ever", "re_entry"])
                else:
                    self.assertIsNone(act.get("position_entry_type"))


if __name__ == "__main__":
    unittest.main()
