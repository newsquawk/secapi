"""
tests/test_security_classification.py

Tests for New Ask #2: Security Classification.
Verifies:
1. Every activity item exposes `security_category` ('COMMON', 'ETF', 'OPTION', 'CONVERTIBLE', 'OTHER').
2. Stock activity items are appropriately categorized as COMMON, ETF, CONVERTIBLE, or OTHER.
3. Option activity items are categorized as OPTION.
4. Filtering `/activity/latest/v3?security_category=ETF` returns strictly ETF activities.
5. Filtering `/activity/latest/v3?security_category=COMMON` returns strictly COMMON stock activities.
6. Passing an invalid `security_category` returns HTTP 400 with a descriptive error.
7. Change feed `/changes` exposes `security_category` on every item's activities.
8. Holding detail endpoint `/filings/{accession_number}` exposes `security_category` on holdings.
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
from fastapi.testclient import TestClient
from main import app
from auth import sync_dependencies


class TestSecurityClassification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_stock_activity_exposes_security_category(self):
        """Verify GET /activity/latest/v3 exposes security_category in response."""
        r = self.client.get("/activity/latest/v3?security_type=stocks&limit=5")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        activities = data.get("activities", [])
        self.assertGreater(len(activities), 0)
        for act in activities:
            self.assertIn("security_category", act)
            self.assertIn(act["security_category"], {"COMMON", "ETF", "CONVERTIBLE", "OTHER"})

    def test_options_activity_has_option_security_category(self):
        """Verify GET /activity/latest/options exposes security_category='OPTION'."""
        r = self.client.get("/activity/latest/options?limit=5")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        activities = data.get("activities", [])
        self.assertGreater(len(activities), 0)
        for act in activities:
            self.assertEqual(act.get("security_category"), "OPTION")

    def test_filter_by_security_category_etf(self):
        """Verify GET /activity/latest/v3?security_category=ETF filters strictly to ETFs."""
        r = self.client.get("/activity/latest/v3?security_category=ETF&limit=10")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        activities = data.get("activities", [])
        for act in activities:
            self.assertEqual(act.get("security_category"), "ETF")

    def test_filter_by_security_category_common(self):
        """Verify GET /activity/latest/v3?security_category=COMMON filters strictly to COMMON stocks."""
        r = self.client.get("/activity/latest/v3?security_category=COMMON&limit=10")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        activities = data.get("activities", [])
        self.assertGreater(len(activities), 0)
        for act in activities:
            self.assertEqual(act.get("security_category"), "COMMON")
            self.assertTrue(act.get("is_common_stock"))

    def test_invalid_security_category_returns_400(self):
        """Verify passing an unknown security_category returns HTTP 400 Bad Request."""
        r = self.client.get("/activity/latest/v3?security_category=CRYPTOCURRENCY")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Invalid security_category", r.json().get("detail", ""))

    def test_change_feed_exposes_security_category(self):
        """Verify GET /changes includes security_category on every activity in the envelope."""
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "service-account"}
        try:
            r = self.client.get("/changes?limit=2")
            self.assertEqual(r.status_code, 200)
            items = r.json().get("items", [])
            self.assertGreater(len(items), 0)
            all_activities = []
            for item in items:
                all_activities.extend(item.get("activities", []))
            self.assertGreater(len(all_activities), 0)
            for act in all_activities:
                self.assertIn("security_category", act)
                self.assertIn(act["security_category"], {"COMMON", "ETF", "OPTION", "CONVERTIBLE", "OTHER"})
        finally:
            app.dependency_overrides.clear()

    def test_filing_holdings_detail_exposes_security_category(self):
        """Verify GET /filings/{accession_number} includes security_category in holding objects."""
        # Fetch a recent filing accession number
        r_filings = self.client.get("/filings?limit=1")
        self.assertEqual(r_filings.status_code, 200)
        filings = r_filings.json().get("filings", [])
        self.assertGreater(len(filings), 0)
        acc_num = filings[0]["accession_number"]

        r_detail = self.client.get(f"/filings/{acc_num}?length=5")
        self.assertEqual(r_detail.status_code, 200)
        holdings = r_detail.json().get("data", [])
        if holdings:
            for h in holdings:
                self.assertIn("security_category", h)
                if h["security_category"] is not None:
                    self.assertIn(h["security_category"], {"COMMON", "ETF", "OPTION", "CONVERTIBLE", "OTHER"})


if __name__ == "__main__":
    unittest.main()
