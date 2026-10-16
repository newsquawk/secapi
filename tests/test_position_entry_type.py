"""
tests/test_position_entry_type.py

Comprehensive test suite for Milestone 1: R1 First-Ever vs. Re-entry Position Flag.
Covers:
  1. Unit tests for holding_enricher.enrich_activities (nullability, aliases, case-insensitivity, models & dicts)
  2. Integration tests with live PostgreSQL data (Berkshire Hathaway, Inspire Investing, Marshall Wace)
  3. API Endpoint verification:
     - GET /changes
     - GET /activity/latest/v3
     - GET /activity/latest/options
     - GET /stories/latest/v2
  4. Performance guardrail: bulk enrichment of large batches with zero N+1 bottlenecks.
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


class TestPositionEntryType(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        # Override sync auth dependencies for testing /changes
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "test-service-account"}

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    # =========================================================================
    # 1. UNIT TESTS: holding_enricher.enrich_activities
    # =========================================================================

    def test_enrich_empty_activities(self):
        """Empty list should return empty list immediately without error."""
        conn = get_db_connection()
        try:
            res = enrich_activities(conn, [])
            self.assertEqual(res, [])
        finally:
            conn.close()

    def test_non_new_positions_strictly_none(self):
        """All non-new positions (increased, decreased, unchanged, closed) MUST have None."""
        conn = get_db_connection()
        try:
            test_items = [
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "037833100", "change_type": "increased"},
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "037833100", "change_type": "decreased"},
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "037833100", "change_type": "unchanged"},
                {"company_id": 58, "reporting_period": dt.date(2026, 6, 30), "cusip": "037833100", "change_type": "closed"},
            ]
            enrich_activities(conn, test_items)
            for item in test_items:
                self.assertIsNone(item["position_entry_type"], f"Failed for {item['change_type']}")
                self.assertIsNone(item["entry_type"], f"Failed for {item['change_type']}")
        finally:
            conn.close()

    def test_re_entry_vs_first_ever_classification(self):
        """
        Verify that holding_enricher correctly distinguishes re_entry from first_ever:
        - Berkshire (company_id=58) held DR Horton (23331A109) in 2025 -> re_entry for 2026-06-30
        - Inspire Investing (company_id=8333) never held Affirm (00827B106) prior to 2026-06-30 -> first_ever
        """
        conn = get_db_connection()
        try:
            items = [
                {
                    "company_id": 58,
                    "reporting_period": dt.date(2026, 6, 30),
                    "cusip": "23331A109",
                    "change_type": "new",
                },
                {
                    "company_id": 8333,
                    "reporting_period": dt.date(2026, 6, 30),
                    "cusip": "00827B106",
                    "change_type": "new",
                },
            ]
            enrich_activities(conn, items)
            self.assertEqual(items[0]["position_entry_type"], "re_entry")
            self.assertEqual(items[0]["entry_type"], "re_entry")
            self.assertEqual(items[1]["position_entry_type"], "first_ever")
            self.assertEqual(items[1]["entry_type"], "first_ever")
        finally:
            conn.close()

    def test_cusip_case_insensitivity(self):
        """CUSIP comparison must be case-insensitive (lowercase cusip matches DB)."""
        conn = get_db_connection()
        try:
            items = [
                {
                    "company_id": 58,
                    "reporting_period": dt.date(2026, 6, 30),
                    "cusip": "23331a109",  # lowercase
                    "change_type": "new",
                }
            ]
            enrich_activities(conn, items)
            self.assertEqual(items[0]["position_entry_type"], "re_entry")
            self.assertEqual(items[0]["entry_type"], "re_entry")
        finally:
            conn.close()

    def test_cik_resolution_when_company_id_missing(self):
        """When company_id is not provided, CIK should be resolved automatically."""
        conn = get_db_connection()
        try:
            items = [
                {
                    "cik": "0001067983",  # Berkshire Hathaway
                    "reporting_period": "2026-06-30",
                    "cusip": "23331A109",
                    "change_type": "new",
                }
            ]
            enrich_activities(conn, items)
            self.assertEqual(items[0]["position_entry_type"], "re_entry")
            self.assertEqual(items[0]["entry_type"], "re_entry")
        finally:
            conn.close()

    def test_holding_activity_model_support(self):
        """enrich_activities must support Pydantic HoldingActivity model instances."""
        conn = get_db_connection()
        try:
            h1 = HoldingActivity(
                cik="0001067983",
                company_name="BERKSHIRE HATHAWAY INC",
                latest_accession_number="0001067983-26-000001",
                reporting_period=dt.date(2026, 6, 30),
                filing_date=dt.date(2026, 8, 14),
                issuer_name="D R HORTON INC",
                cusip="23331A109",
                is_common_stock=True,
                change_type="new",
                absolute_value_change=50000000,
            )
            h2 = HoldingActivity(
                cik="0001067983",
                company_name="BERKSHIRE HATHAWAY INC",
                latest_accession_number="0001067983-26-000001",
                reporting_period=dt.date(2026, 6, 30),
                filing_date=dt.date(2026, 8, 14),
                issuer_name="APPLE INC",
                cusip="037833100",
                is_common_stock=True,
                change_type="increased",
                absolute_value_change=100000000,
            )
            enrich_activities(conn, [h1, h2])
            self.assertEqual(h1.position_entry_type, "re_entry")
            self.assertEqual(h1.entry_type, "re_entry")
            self.assertIsNone(h2.position_entry_type)
            self.assertIsNone(h2.entry_type)
        finally:
            conn.close()

    # =========================================================================
    # 2. INTEGRATION TESTS: Live Database Queries
    # =========================================================================

    def test_live_berkshire_reentry(self):
        """Verify Berkshire Hathaway real database records for re_entry."""
        res = self.client.get("/activity/latest/v3?cik=0001067983&ticker=DHI")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        activities = data.get("activities", [])
        new_dhi = [a for a in activities if a.get("change_type") == "new" and "23331A109" in a.get("cusip", "")]
        for act in new_dhi:
            self.assertEqual(act["position_entry_type"], "re_entry")
            self.assertEqual(act["entry_type"], "re_entry")

    def test_live_inspire_first_ever(self):
        """Verify Inspire Investing real database records for first_ever."""
        res = self.client.get("/activity/latest/v3?cik=0001731927&ticker=AFRM")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        activities = data.get("activities", [])
        new_afrm = [a for a in activities if a.get("change_type") == "new" and "00827B106" in a.get("cusip", "")]
        for act in new_afrm:
            self.assertEqual(act["position_entry_type"], "first_ever")
            self.assertEqual(act["entry_type"], "first_ever")

    # =========================================================================
    # 3. ENDPOINT TESTS: /activity/latest/v3, /options, /changes, /stories
    # =========================================================================

    def test_endpoint_activity_latest_v3_schema(self):
        """Verify /activity/latest/v3 response schema exposes entry type fields correctly."""
        res = self.client.get("/activity/latest/v3?limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        activities = data.get("activities", [])
        self.assertGreater(len(activities), 0)

        for act in activities:
            self.assertIn("position_entry_type", act)
            self.assertIn("entry_type", act)
            self.assertEqual(act["position_entry_type"], act["entry_type"])
            if act["change_type"] == "new":
                self.assertIn(act["position_entry_type"], ("first_ever", "re_entry"))
            else:
                self.assertIsNone(act["position_entry_type"])

    def test_endpoint_activity_latest_options_schema(self):
        """Verify /activity/latest/options response schema exposes entry type fields."""
        res = self.client.get("/activity/latest/options?limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        activities = data.get("activities", [])
        if activities:
            for act in activities:
                self.assertIn("position_entry_type", act)
                self.assertIn("entry_type", act)
                self.assertEqual(act["position_entry_type"], act["entry_type"])
                if act["change_type"] == "new":
                    self.assertIn(act["position_entry_type"], ("first_ever", "re_entry"))
                else:
                    self.assertIsNone(act["position_entry_type"])

    def test_endpoint_changes_schema(self):
        """Verify /changes feed items embed activities with entry type fields."""
        res = self.client.get("/changes?limit=2")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        items = data.get("items", [])
        self.assertGreater(len(items), 0)

        total_activities = 0
        for env in items:
            for act in env.get("activities", []):
                total_activities += 1
                self.assertIn("position_entry_type", act)
                self.assertIn("entry_type", act)
                self.assertEqual(act["position_entry_type"], act["entry_type"])
                if act["change_type"] == "new":
                    self.assertIn(act["position_entry_type"], ("first_ever", "re_entry"))
                else:
                    self.assertIsNone(act["position_entry_type"])
        self.assertGreater(total_activities, 0)

    def test_endpoint_stories_latest_v2_schema(self):
        """Verify /stories/latest/v2 SignificantHolding models carry entry type fields."""
        res = self.client.get("/stories/latest/v2?limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        stories = data.get("stories", [])
        self.assertGreater(len(stories), 0)

        for story in stories:
            top_new = story.get("top_new_position")
            if top_new:
                self.assertIn("position_entry_type", top_new)
                self.assertIn("entry_type", top_new)
                self.assertEqual(top_new["position_entry_type"], top_new["entry_type"])
                self.assertIn(top_new["position_entry_type"], ("first_ever", "re_entry"))

            top_new_other = story.get("top_new_other_securities")
            if top_new_other:
                self.assertIn("position_entry_type", top_new_other)
                self.assertIn("entry_type", top_new_other)
                self.assertEqual(top_new_other["position_entry_type"], top_new_other["entry_type"])
                self.assertIn(top_new_other["position_entry_type"], ("first_ever", "re_entry"))

            for closed_key in ("top_closed_position", "top_closed_other_securities"):
                closed = story.get(closed_key)
                if closed:
                    self.assertIsNone(closed.get("position_entry_type"))
                    self.assertIsNone(closed.get("entry_type"))

            for change_key in (
                "top_increased_position",
                "top_decreased_position",
                "top_increased_other_securities",
                "top_decreased_other_securities",
            ):
                change = story.get(change_key)
                if change:
                    self.assertIsNone(change.get("position_entry_type"))
                    self.assertIsNone(change.get("entry_type"))

    # =========================================================================
    # 4. PERFORMANCE GUARDRAIL: Zero N+1 Query Bottleneck
    # =========================================================================

    def test_bulk_enrichment_performance(self):
        """Bulk enrichment of 100 candidate activities must complete under 100 ms."""
        conn = get_db_connection()
        try:
            # Generate 100 candidate items across multiple companies
            candidates = [
                {
                    "company_id": 58 if i % 2 == 0 else 8333,
                    "reporting_period": dt.date(2026, 6, 30),
                    "cusip": "23331A109" if i % 2 == 0 else "00827B106",
                    "change_type": "new",
                }
                for i in range(100)
            ]
            t0 = time.time()
            enrich_activities(conn, candidates)
            duration_ms = (time.time() - t0) * 1000
            self.assertLess(duration_ms, 100.0, f"Enrichment took {duration_ms:.2f}ms, expected < 100ms")
            for c in candidates:
                self.assertIn(c["position_entry_type"], ("first_ever", "re_entry"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
