"""
tests/test_flow_top_changes.py

Unit tests for the GET /api/v1/flow/top-changes endpoint:
- Case-insensitivity & CUSIP normalization (no lowercase or duplicate CUSIPs)
- Sort by shares vs sort by value
- Validation of sort_by parameter
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
from fastapi.testclient import TestClient
from main import app


class TestFlowTopChanges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_top_changes_cusip_normalization_and_uniqueness(self):
        """Verify that all returned CUSIPs are uppercase and there are no duplicates."""
        response = self.client.get("/api/v1/flow/top-changes?date=2026-08-26&sort_by=shares")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(data["sort_by"], "shares")
        stocks = data.get("stocks", [])
        self.assertGreater(len(stocks), 0)

        seen_cusips = set()
        for stock in stocks:
            cusip = stock.get("cusip")
            if cusip:
                # Must be strictly uppercase
                self.assertEqual(cusip, cusip.upper(), f"CUSIP {cusip} is not uppercase")
                # Must be unique in the output (no lowercase/uppercase split)
                self.assertNotIn(cusip, seen_cusips, f"Duplicate CUSIP found in response: {cusip}")
                seen_cusips.add(cusip)

    def test_top_changes_sort_by_value(self):
        """Verify sorting by value returns valid results."""
        response = self.client.get("/api/v1/flow/top-changes?date=2026-08-26&sort_by=value")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["sort_by"], "value")
        self.assertGreater(len(data.get("stocks", [])), 0)

    def test_top_changes_invalid_sort_by(self):
        """Verify 400 Bad Request for invalid sort_by option."""
        response = self.client.get("/api/v1/flow/top-changes?date=2026-08-26&sort_by=invalid")
        self.assertEqual(response.status_code, 400)
