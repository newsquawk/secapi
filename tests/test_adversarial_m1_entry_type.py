"""
tests/test_adversarial_m1_entry_type.py

Adversarial Fuzzing and Invariant Stress-Testing Suite for Milestone 1 (R1 Position Entry Type).
Authored by Empirical Challenger (challenger_m1_2).

This suite tests:
1. Invariant: position_entry_type == entry_type on 100% of responses across all endpoints.
2. Invariant: position_entry_type is not None IF AND ONLY IF change_type == 'new'.
3. Invariant: position_entry_type in ('first_ever', 're_entry') whenever non-null.
4. Invariant: Under options (/activity/latest/options), options classified as 'new' carry valid entry flags without crashes.
5. Error conditions: empty lists, invalid CIKs, malformed CUSIPs, SQL injection payloads, missing fields.
6. Institutional Oracle Verification: direct PostgreSQL historical query oracle vs enrich_activities.
7. Edge-case fragility boundaries (string company_id, non-string cusip, transaction state safety).
"""

import datetime as dt
import os
import time
import unittest
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from auth import sync_dependencies
from database import get_db_connection
from holding_enricher import enrich_activities
from main import app
from sec_models import HoldingActivity


class TestAdversarialM1EntryType(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "test-service-account"}

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    # =========================================================================
    # 1. INVARIANT 1: position_entry_type == entry_type ON 100% OF RESPONSES
    # =========================================================================

    def test_invariant_1_strict_parity_stocks_activity(self):
        """Invariant 1: position_entry_type == entry_type across /activity/latest/v3."""
        for offset in [0, 50, 100]:
            res = self.client.get(f"/activity/latest/v3?limit=50&offset={offset}")
            self.assertEqual(res.status_code, 200)
            activities = res.json().get("activities", [])
            for act in activities:
                self.assertIn("position_entry_type", act)
                self.assertIn("entry_type", act)
                self.assertEqual(
                    act["position_entry_type"],
                    act["entry_type"],
                    f"Mismatch in /activity/latest/v3: {act['position_entry_type']} != {act['entry_type']}",
                )

    def test_invariant_1_strict_parity_options_activity(self):
        """Invariant 1: position_entry_type == entry_type across /activity/latest/options."""
        for offset in [0, 50]:
            res = self.client.get(f"/activity/latest/options?limit=50&offset={offset}")
            self.assertEqual(res.status_code, 200)
            activities = res.json().get("activities", [])
            for act in activities:
                self.assertIn("position_entry_type", act)
                self.assertIn("entry_type", act)
                self.assertEqual(
                    act["position_entry_type"],
                    act["entry_type"],
                    f"Mismatch in /activity/latest/options: {act['position_entry_type']} != {act['entry_type']}",
                )

    def test_invariant_1_strict_parity_changes_feed(self):
        """Invariant 1: position_entry_type == entry_type across /changes feed items."""
        res = self.client.get("/changes?limit=5")
        self.assertEqual(res.status_code, 200)
        items = res.json().get("items", [])
        for env in items:
            for act in env.get("activities", []):
                self.assertIn("position_entry_type", act)
                self.assertIn("entry_type", act)
                self.assertEqual(
                    act["position_entry_type"],
                    act["entry_type"],
                    f"Mismatch in /changes: {act['position_entry_type']} != {act['entry_type']}",
                )

    def test_invariant_1_strict_parity_stories_v2(self):
        """Invariant 1: position_entry_type == entry_type across /stories/latest/v2."""
        res = self.client.get("/stories/latest/v2?limit=10")
        self.assertEqual(res.status_code, 200)
        stories = res.json().get("stories", [])
        for s in stories:
            for key in ["top_new_position", "top_new_other_securities"]:
                h = s.get(key)
                if h:
                    self.assertIn("position_entry_type", h)
                    self.assertIn("entry_type", h)
                    self.assertEqual(
                        h["position_entry_type"],
                        h["entry_type"],
                        f"Mismatch in stories {key}: {h['position_entry_type']} != {h['entry_type']}",
                    )

    # =========================================================================
    # 2. INVARIANT 2: position_entry_type is not None IFF change_type == 'new'
    # =========================================================================

    def test_invariant_2_nullability_biconditional_stocks(self):
        """
        Invariant 2: position_entry_type is not None <=> change_type == 'new'.
        Verified on large sample of stocks activity.
        """
        res = self.client.get("/activity/latest/v3?limit=50&security_type=stocks")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        self.assertGreater(len(activities), 0)

        for act in activities:
            ct = act.get("change_type")
            pet = act.get("position_entry_type")
            if ct == "new":
                self.assertIsNotNone(
                    pet,
                    f"Invariant 2 failure: new position has null position_entry_type ({act.get('cusip')})",
                )
            else:
                self.assertIsNone(
                    pet,
                    f"Invariant 2 failure: non-new position ({ct}) has non-null position_entry_type ({pet})",
                )

    def test_invariant_2_nullability_biconditional_options(self):
        """
        Invariant 2: position_entry_type is not None <=> change_type == 'new'.
        Verified on options activity.
        """
        res = self.client.get("/activity/latest/options?limit=50")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        self.assertGreater(len(activities), 0)

        for act in activities:
            ct = act.get("change_type")
            pet = act.get("position_entry_type")
            if ct == "new":
                self.assertIsNotNone(
                    pet,
                    f"Invariant 2 failure on option: new option has null position_entry_type ({act.get('cusip')})",
                )
            else:
                self.assertIsNone(
                    pet,
                    f"Invariant 2 failure on option: non-new option ({ct}) has non-null position_entry_type ({pet})",
                )

    def test_invariant_2_nullability_all_non_new_types(self):
        """
        Verify each non-new change_type explicitly in isolation:
        increased, decreased, closed, unchanged must all result in None.
        """
        conn = get_db_connection()
        try:
            items = [
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "037833100", "change_type": "increased"},
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "037833100", "change_type": "decreased"},
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "037833100", "change_type": "closed"},
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "037833100", "change_type": "unchanged"},
                # Also test uppercase and whitespace variants
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "037833100", "change_type": " INCREASED "},
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "037833100", "change_type": "DECREASED"},
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "037833100", "change_type": "Closed"},
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "037833100", "change_type": "Unchanged"},
            ]
            enrich_activities(conn, items)
            for item in items:
                self.assertIsNone(item["position_entry_type"], f"Expected None for {item['change_type']}")
                self.assertIsNone(item["entry_type"], f"Expected None for {item['change_type']}")
        finally:
            conn.close()

    # =========================================================================
    # 3. INVARIANT 3: position_entry_type in ('first_ever', 're_entry')
    # =========================================================================

    def test_invariant_3_domain_values_across_endpoints(self):
        """Invariant 3: Any non-null position_entry_type must be strictly 'first_ever' or 're_entry'."""
        endpoints = [
            "/activity/latest/v3?limit=50",
            "/activity/latest/options?limit=50",
            "/changes?limit=5",
        ]
        valid_domains = {"first_ever", "re_entry"}

        for ep in endpoints:
            res = self.client.get(ep)
            self.assertEqual(res.status_code, 200)
            data = res.json()
            if "activities" in data:
                activities = data["activities"]
            elif "items" in data:
                activities = [a for env in data["items"] for a in env.get("activities", [])]
            else:
                activities = []

            for act in activities:
                pet = act.get("position_entry_type")
                if pet is not None:
                    self.assertIn(
                        pet,
                        valid_domains,
                        f"Invalid position_entry_type '{pet}' on endpoint {ep}",
                    )

    # =========================================================================
    # 4. INVARIANT 4: OPTIONS ACTIVITY ROBUSTNESS & ACCURACY
    # =========================================================================

    def test_invariant_4_options_new_positions_have_valid_flags(self):
        """Options classified as 'new' must carry valid entry flags without crashes."""
        res = self.client.get("/activity/latest/options?change_type=new&limit=50")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        self.assertGreater(len(activities), 0, "Expected new option activities")

        for act in activities:
            self.assertEqual(act.get("change_type"), "new")
            self.assertIn(act.get("position_entry_type"), ("first_ever", "re_entry"))
            self.assertEqual(act.get("position_entry_type"), act.get("entry_type"))
            self.assertEqual(act.get("security_category"), "OPTION")

    def test_invariant_4_options_puts_and_calls_support(self):
        """Both puts and calls with change_type == 'new' must be enriched without crashes."""
        for poc in ["put", "call"]:
            res = self.client.get(f"/activity/latest/options?put_or_call={poc}&limit=25")
            self.assertEqual(res.status_code, 200, f"Options query failed for put_or_call={poc}")
            activities = res.json().get("activities", [])
            for act in activities:
                self.assertEqual(act.get("position_entry_type"), act.get("entry_type"))
                if act.get("change_type") == "new":
                    self.assertIn(act.get("position_entry_type"), ("first_ever", "re_entry"))
                else:
                    self.assertIsNone(act.get("position_entry_type"))

    def test_invariant_4_options_ticker_filtering(self):
        """Options filtered by high-volume tickers (AAPL, SPY) enrich properly."""
        for ticker in ["AAPL", "SPY"]:
            res = self.client.get(f"/activity/latest/options?ticker={ticker}")
            self.assertEqual(res.status_code, 200)
            activities = res.json().get("activities", [])
            for act in activities:
                self.assertEqual(act.get("position_entry_type"), act.get("entry_type"))
                if act.get("change_type") == "new":
                    self.assertIn(act.get("position_entry_type"), ("first_ever", "re_entry"))
                else:
                    self.assertIsNone(act.get("position_entry_type"))

    # =========================================================================
    # 5. ERROR CONDITIONS & ADVERSARIAL EDGE CASES
    # =========================================================================

    def test_adversarial_empty_and_trivial_inputs(self):
        """Test empty list and single-empty-item edge cases."""
        conn = get_db_connection()
        try:
            # 1. Empty list
            res1 = enrich_activities(conn, [])
            self.assertEqual(res1, [])

            # 2. Empty dict item
            items = [{}]
            enrich_activities(conn, items)
            self.assertIsNone(items[0].get("position_entry_type"))
            self.assertIsNone(items[0].get("entry_type"))

            # 3. Item with change_type new but missing all other fields
            items_missing = [{"change_type": "new"}]
            enrich_activities(conn, items_missing)
            self.assertEqual(items_missing[0].get("position_entry_type"), "first_ever")
            self.assertEqual(items_missing[0].get("entry_type"), "first_ever")
        finally:
            conn.close()

    def test_adversarial_invalid_ciks(self):
        """Test non-numeric, non-existent, and malformed CIK inputs."""
        conn = get_db_connection()
        try:
            items = [
                # Non-existent CIK
                {"cik": "9999999999", "cusip": "037833100", "reporting_period": "2026-06-30", "change_type": "new"},
                # Alpha CIK
                {"cik": "INVALID_CIK", "cusip": "037833100", "reporting_period": "2026-06-30", "change_type": "new"},
                # All zeros
                {"cik": "0000000000", "cusip": "037833100", "reporting_period": "2026-06-30", "change_type": "new"},
                # Negative CIK
                {"cik": "-12345", "cusip": "037833100", "reporting_period": "2026-06-30", "change_type": "new"},
            ]
            enrich_activities(conn, items)
            for item in items:
                # When CIK cannot resolve to a company_id, it defaults safely to 'first_ever'
                self.assertEqual(item.get("position_entry_type"), "first_ever")
                self.assertEqual(item.get("entry_type"), "first_ever")
        finally:
            conn.close()

    def test_adversarial_malformed_cusips(self):
        """Test strange, short, long, and malformed CUSIPs."""
        conn = get_db_connection()
        try:
            items = [
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "", "change_type": "new"},
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "   ", "change_type": "new"},
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "SHORT", "change_type": "new"},
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "A" * 50, "change_type": "new"},
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "@@@$$$###", "change_type": "new"},
            ]
            enrich_activities(conn, items)
            for item in items:
                # Malformed CUSIPs with no historical match safely resolve to 'first_ever'
                self.assertEqual(item.get("position_entry_type"), "first_ever")
                self.assertEqual(item.get("entry_type"), "first_ever")
        finally:
            conn.close()

    def test_adversarial_sql_injection_defense(self):
        """Verify that SQL injection payloads in CIK or CUSIP do not breach SQL execution."""
        conn = get_db_connection()
        try:
            items = [
                {"cik": "' OR '1'='1", "cusip": "037833100", "reporting_period": "2026-06-30", "change_type": "new"},
                {"company_id": 58, "cusip": "' OR '1'='1", "reporting_period": "2026-06-30", "change_type": "new"},
                {"company_id": 58, "cusip": "037833100'; DROP TABLE companies; --", "reporting_period": "2026-06-30", "change_type": "new"},
                {"company_id": 58, "cusip": "037833100' UNION SELECT 1, 'evil', '2026-06-30' --", "reporting_period": "2026-06-30", "change_type": "new"},
            ]
            enrich_activities(conn, items)
            for item in items:
                self.assertIn(item.get("position_entry_type"), ("first_ever", "re_entry"))
                self.assertEqual(item.get("position_entry_type"), item.get("entry_type"))
        finally:
            conn.close()

    def test_adversarial_malformed_dates(self):
        """Verify handling of invalid date representations."""
        conn = get_db_connection()
        try:
            items = [
                {"company_id": 58, "reporting_period": "invalid-date-format", "cusip": "23331A109", "change_type": "new"},
                {"company_id": 58, "reporting_period": "2026-99-99", "cusip": "23331A109", "change_type": "new"},
                {"company_id": 58, "reporting_period": None, "cusip": "23331A109", "change_type": "new"},
            ]
            enrich_activities(conn, items)
            for item in items:
                # If date cannot be parsed, defaults gracefully to 'first_ever'
                self.assertEqual(item.get("position_entry_type"), "first_ever")
                self.assertEqual(item.get("entry_type"), "first_ever")
        finally:
            conn.close()

    def test_adversarial_unknown_change_type(self):
        """Verify that strange or unknown change_types are treated as non-new and set to None."""
        conn = get_db_connection()
        try:
            items = [
                {"company_id": 58, "reporting_period": "2026-06-30", "cusip": "23331A109", "change_type": "split"},
                {"company_id": 58, "reporting_period": "2026-06-30", "cusip": "23331A109", "change_type": "rebalanced"},
                {"company_id": 58, "reporting_period": "2026-06-30", "cusip": "23331A109", "change_type": ""},
                {"company_id": 58, "reporting_period": "2026-06-30", "cusip": "23331A109", "change_type": None},
            ]
            enrich_activities(conn, items)
            for item in items:
                self.assertIsNone(item.get("position_entry_type"))
                self.assertIsNone(item.get("entry_type"))
        finally:
            conn.close()

    # =========================================================================
    # 6. ORACLE VERIFICATION: GROUND TRUTH HISTORICAL QUERIES
    # =========================================================================

    def test_oracle_ground_truth_cross_check(self):
        """
        Adversarial Oracle:
        Directly query PostgreSQL for distinct filers and their prior holdings,
        verifying that enrich_activities reproduces the exact ground truth.
        """
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            # 1. Oracle for Berkshire Hathaway (company_id=58) DR Horton (23331A109)
            cur.execute("""
                SELECT count(*)
                FROM filings f
                JOIN holdings h ON f.filing_id = h.filing_id
                JOIN issuers i ON h.issuer_id = i.issuer_id
                WHERE f.company_id = 58
                  AND UPPER(i.cusip) = '23331A109'
                  AND f.period_of_report < '2026-06-30';
            """)
            dhi_prior_count = cur.fetchone()[0]
            self.assertGreater(dhi_prior_count, 0, "Berkshire must have prior DHI holdings")

            item_dhi = [
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "23331A109", "change_type": "new"}
            ]
            enrich_activities(conn, item_dhi)
            self.assertEqual(item_dhi[0]["position_entry_type"], "re_entry")

            # 2. Oracle for Inspire Investing (company_id=8333) Affirm (00827B106)
            cur.execute("""
                SELECT count(*)
                FROM filings f
                JOIN holdings h ON f.filing_id = h.filing_id
                JOIN issuers i ON h.issuer_id = i.issuer_id
                WHERE f.company_id = 8333
                  AND UPPER(i.cusip) = '00827B106'
                  AND f.period_of_report < '2026-06-30';
            """)
            afrm_prior_count = cur.fetchone()[0]
            self.assertEqual(afrm_prior_count, 0, "Inspire Investing must have 0 prior AFRM holdings")

            item_afrm = [
                {"company_id": 8333, "reporting_period": dt.date(2026, 6, 30), "cusip": "00827B106", "change_type": "new"}
            ]
            enrich_activities(conn, item_afrm)
            self.assertEqual(item_afrm[0]["position_entry_type"], "first_ever")
            cur.close()
        finally:
            conn.close()

    # =========================================================================
    # 7. HIGH-LOAD ADVERSARIAL STRESS TEST
    # =========================================================================

    def test_adversarial_high_load_fuzzing_batch(self):
        """
        Stress test: 500 mixed activities (new, increased, malformed, various companies)
        processed in a single call to enrich_activities under 250ms.
        """
        conn = get_db_connection()
        try:
            candidates: List[Dict[str, Any]] = []
            for i in range(500):
                if i % 3 == 0:
                    candidates.append({
                        "company_id": 58,
                        "reporting_period": dt.date(2026, 6, 30),
                        "cusip": "23331A109",
                        "change_type": "new",
                    })
                elif i % 3 == 1:
                    candidates.append({
                        "company_id": 8333,
                        "reporting_period": dt.date(2026, 6, 30),
                        "cusip": "00827B106",
                        "change_type": "new",
                    })
                else:
                    candidates.append({
                        "company_id": 58,
                        "reporting_period": dt.date(2026, 6, 30),
                        "cusip": "037833100",
                        "change_type": "increased",
                    })

            t0 = time.time()
            enrich_activities(conn, candidates)
            duration_ms = (time.time() - t0) * 1000
            self.assertLess(duration_ms, 250.0, f"High load took {duration_ms:.2f}ms, expected < 250ms")

            # Check invariants across all 500
            for c in candidates:
                self.assertEqual(c["position_entry_type"], c["entry_type"])
                if c["change_type"] == "new":
                    self.assertIn(c["position_entry_type"], ("first_ever", "re_entry"))
                else:
                    self.assertIsNone(c["position_entry_type"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
