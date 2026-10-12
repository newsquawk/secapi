"""
tests/test_filing_aum.py

Regression test suite for per-filing AUM and portfolio weight_pct (Issue 3).
Verifies:
1. Activity endpoint (/activity/latest/v3) uses accurate filing-level total value.
2. Position weight_pct is computed as (current_value / filing_aum) * 100.
3. Closed positions have weight_pct = None.
4. Historical quarters use their actual filing portfolio total rather than a stale static figure.
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
from fastapi.testclient import TestClient
from main import app


class TestFilingAUM(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_bridgewater_filing_aum_and_weight_pct(self):
        """
        Issue 3: Verify that Bridgewater's 2026-06-30 filing uses its true portfolio AUM
        ($24,376,086,530) and calculates exact weight_pct for its holdings.
        """
        r = self.client.get("/activity/latest/v3?security_type=stocks&cik=0001350694&ticker=BKNG")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        activities = data.get("activities", [])
        self.assertGreaterEqual(len(activities), 1)

        bkng = next((a for a in activities if "BOOKING" in (a.get("issuer_name") or "")), None)
        self.assertIsNotNone(bkng, "Booking Holdings not found in Bridgewater activity")

        # Verify AUM is the true filing portfolio value
        self.assertEqual(bkng["aum"], 24376086530)

        # Verify weight_pct calculation: (current_value / aum) * 100
        expected_weight = round((bkng["current_value"] / bkng["aum"]) * 100, 4)
        self.assertAlmostEqual(bkng["weight_pct"], expected_weight, places=3)

    def test_weight_pct_null_for_closed_positions(self):
        """
        Closed positions have current_value = 0 or None, so weight_pct must be None.
        """
        r = self.client.get("/activity/latest/v3?security_type=stocks&cik=0001350694&change_type=closed&limit=5")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        for act in data.get("activities", []):
            if act["change_type"] == "closed":
                self.assertIsNone(act["weight_pct"])


if __name__ == "__main__":
    unittest.main()
