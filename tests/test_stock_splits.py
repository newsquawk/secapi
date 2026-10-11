"""
tests/test_stock_splits.py

Regression test suite for stock split normalisation across quarter-over-quarter
comparisons in /activity/latest/v3 and the change feed (Issue 2).
Covers:
1. Rebased previous shares and split factor (e.g. 25:1 for Booking Holdings BKNG).
2. Elimination of false +77,824% share increases down to true split-adjusted deltas.
3. Rebased previous price per share matching actual underlying price movements.
4. Carvana (CVNA) 5:1 split factor detection.
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
from fastapi.testclient import TestClient
from main import app


class TestStockSplits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_booking_holdings_split_adjustment_bridgewater(self):
        """
        Issue 2: Booking Holdings (BKNG) executed a 25:1 forward split in June 2026.
        Bridgewater held 54 shares in Q1 and 42,079 in Q2.
        Without split adjustment, secapi reported +77,824% share surge.
        With split adjustment (rebased prev shares = 54 * 25 = 1,350),
        percent_change should be ~3,016.96%, split_factor should be 25.0,
        and previous price per share should rebase from $4,210 to ~$168.41.
        """
        r = self.client.get("/activity/latest/v3?security_type=stocks&cik=0001350694&ticker=BKNG")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        activities = data.get("activities", [])
        self.assertGreaterEqual(len(activities), 1)

        bkng = next((a for a in activities if "BOOKING" in (a.get("issuer_name") or "")), None)
        self.assertIsNotNone(bkng, "Booking Holdings activity not found for Bridgewater")

        # Verify split factor is exposed
        self.assertEqual(bkng.get("split_factor"), 25.0)

        # Verify percent change is rebased (~3,016%, NOT +77,824%)
        self.assertAlmostEqual(bkng.get("percent_change"), 3016.96, delta=5.0)

        # Verify share change is rebased (42,079 - 1,350 = 40,729)
        self.assertEqual(bkng.get("change_in_share"), 40729)

        # Verify previous price per share is rebased to ~$168.41 (NOT $4,210)
        self.assertAlmostEqual(bkng.get("previous_price_per_share"), 168.41, delta=1.0)
        self.assertAlmostEqual(bkng.get("current_price_per_share"), 178.24, delta=1.0)

    def test_carvana_split_factor_detected(self):
        """Carvana (CVNA) 5:1 split must be detected and recorded in stock_splits."""
        from database import _get_db_connection_params
        import psycopg2
        conn = psycopg2.connect(**_get_db_connection_params())
        with conn.cursor() as cur:
            cur.execute("""
                SELECT split_factor 
                FROM stock_splits 
                WHERE cusip = '146869102' AND period_of_report = '2026-06-30'::date
            """)
            row = cur.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(float(row[0]), 5.0)


if __name__ == "__main__":
    unittest.main()
