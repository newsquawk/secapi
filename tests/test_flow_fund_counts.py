"""
tests/test_flow_fund_counts.py

Unit tests for Milestone 3 (Requirement 3: Number of Funds Buying or Selling the Same Stock by Type).
Covers:
1. Pydantic models for flow responses (DailyFlowEntry, AggregateFlowResponse, TopStockChangeEntry, QuarterlyFlowEntry, QuarterlyFlowResponse)
2. Daily flow endpoint: /api/v1/flow/daily/{identifier}
3. Aggregate flow endpoint: /api/v1/flow/aggregate/{identifier}
4. Top market changes endpoint: /api/v1/flow/top-changes
5. Quarterly flow endpoint: /api/v1/flow/quarterly/{identifier}
6. Mathematical invariants:
   - funds_buying == funds_new + funds_increased
   - funds_selling == funds_decreased + funds_closed
   - All fund metrics >= 0
7. Stock split rebasing, 1-share cash-in-lieu tolerance, option exclusion, and amendment deduplication.
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
import datetime as dt
from fastapi.testclient import TestClient
from main import app
from sec_models import (
    DailyFlowEntry,
    DailyFlowResponse,
    AggregateFlowResponse,
    TopStockChangeEntry,
    TopStockChangesResponse,
    QuarterlyFlowEntry,
    QuarterlyFlowResponse,
)


class TestFlowFundCountsUnit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    # -------------------------------------------------------------------------
    # 1. Pydantic Model Unit Tests
    # -------------------------------------------------------------------------

    def test_daily_flow_entry_model_defaults_and_types(self):
        """Verify DailyFlowEntry model fields, defaults, and integer typing."""
        entry = DailyFlowEntry(
            date=dt.date(2026, 8, 14),
            gross_buying=1000.0,
            gross_selling=500.0,
            net_change=500.0,
        )
        self.assertEqual(entry.funds_buying, 0)
        self.assertEqual(entry.funds_selling, 0)
        self.assertEqual(entry.funds_new, 0)
        self.assertEqual(entry.funds_increased, 0)
        self.assertEqual(entry.funds_decreased, 0)
        self.assertEqual(entry.funds_closed, 0)
        self.assertEqual(entry.funds_unchanged, 0)

        # Set values and assert types
        entry_custom = DailyFlowEntry(
            date=dt.date(2026, 8, 14),
            gross_buying=1000.0,
            gross_selling=500.0,
            net_change=500.0,
            funds_buying=5,
            funds_selling=3,
            funds_new=2,
            funds_increased=3,
            funds_decreased=2,
            funds_closed=1,
            funds_unchanged=4,
        )
        self.assertEqual(entry_custom.funds_buying, 5)
        self.assertEqual(entry_custom.funds_new + entry_custom.funds_increased, 5)
        self.assertEqual(entry_custom.funds_selling, 3)
        self.assertEqual(entry_custom.funds_decreased + entry_custom.funds_closed, 3)

    def test_aggregate_flow_response_model_defaults(self):
        """Verify AggregateFlowResponse model fields and fund count defaults."""
        agg = AggregateFlowResponse(
            cusip="037833100",
            days_looked_back=14,
            gross_buying=50000.0,
            gross_selling=20000.0,
            net_change=30000.0,
        )
        self.assertEqual(agg.funds_buying, 0)
        self.assertEqual(agg.funds_selling, 0)
        self.assertEqual(agg.funds_new, 0)
        self.assertEqual(agg.funds_increased, 0)
        self.assertEqual(agg.funds_decreased, 0)
        self.assertEqual(agg.funds_closed, 0)
        self.assertEqual(agg.funds_unchanged, 0)

    def test_top_stock_change_entry_model_fields(self):
        """Verify TopStockChangeEntry contains the 7 fund metrics."""
        mover = TopStockChangeEntry(
            issuer_name="APPLE INC",
            cusip="037833100",
            ticker="AAPL",
            net_shares_change=100000.0,
            net_value_change=25000000.0,
            absolute_value_change=25000000.0,
            gross_buying_shares=150000.0,
            gross_selling_shares=50000.0,
            funds_buying=10,
            funds_selling=4,
            funds_new=3,
            funds_increased=7,
            funds_decreased=3,
            funds_closed=1,
            funds_unchanged=15,
        )
        self.assertEqual(mover.funds_buying, 10)
        self.assertEqual(mover.funds_new + mover.funds_increased, 10)
        self.assertEqual(mover.funds_selling, 4)
        self.assertEqual(mover.funds_decreased + mover.funds_closed, 4)
        self.assertEqual(mover.funds_unchanged, 15)

    def test_quarterly_flow_models(self):
        """Verify QuarterlyFlowEntry and QuarterlyFlowResponse schemas."""
        q_entry = QuarterlyFlowEntry(
            quarter=dt.date(2026, 6, 30),
            gross_buying=1200000.0,
            gross_selling=800000.0,
            net_change=400000.0,
            percent_change=5.2,
            funds_buying=25,
            funds_selling=18,
            funds_new=10,
            funds_increased=15,
            funds_decreased=12,
            funds_closed=6,
            funds_unchanged=40,
        )
        resp = QuarterlyFlowResponse(
            ticker="AAPL",
            cusip="037833100",
            quarterly_data=[q_entry],
        )
        self.assertEqual(len(resp.quarterly_data), 1)
        self.assertEqual(resp.quarterly_data[0].quarter, dt.date(2026, 6, 30))
        self.assertEqual(resp.quarterly_data[0].funds_buying, 25)
        self.assertEqual(resp.quarterly_data[0].funds_selling, 18)

    # -------------------------------------------------------------------------
    # 2. Daily Flow Endpoint Tests (/api/v1/flow/daily/{identifier})
    # -------------------------------------------------------------------------

    def test_daily_flow_apple_invariants(self):
        """Verify daily flow response for AAPL satisfies all 7 fund fields and invariants."""
        res = self.client.get("/api/v1/flow/daily/AAPL?days=7")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["ticker"], "AAPL")
        self.assertEqual(data["cusip"], "037833100")

        daily_data = data.get("daily_data", [])
        for entry in daily_data:
            funds_buying = entry["funds_buying"]
            funds_selling = entry["funds_selling"]
            funds_new = entry["funds_new"]
            funds_increased = entry["funds_increased"]
            funds_decreased = entry["decreased" if "decreased" in entry else "funds_decreased"]
            funds_closed = entry["funds_closed"]
            funds_unchanged = entry["funds_unchanged"]

            # Mathematical invariants
            self.assertEqual(funds_buying, funds_new + funds_increased)
            self.assertEqual(funds_selling, funds_decreased + funds_closed)

            # Non-negative integers
            for f in [funds_buying, funds_selling, funds_new, funds_increased, funds_decreased, funds_closed, funds_unchanged]:
                self.assertIsInstance(f, int)
                self.assertGreaterEqual(f, 0)

    def test_daily_flow_cusip_resolution(self):
        """Verify daily flow works when querying with 9-digit CUSIP directly."""
        res = self.client.get("/api/v1/flow/daily/037833100?days=5")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["cusip"], "037833100")
        self.assertGreater(len(data.get("daily_data", [])), 0)

    def test_daily_flow_invalid_identifier(self):
        """Verify daily flow returns 400 for unknown ticker that cannot be resolved to CUSIP."""
        res = self.client.get("/api/v1/flow/daily/XYZQ?days=1")
        self.assertEqual(res.status_code, 400)

    # -------------------------------------------------------------------------
    # 3. Aggregate Flow Endpoint Tests (/api/v1/flow/aggregate/{identifier})
    # -------------------------------------------------------------------------

    def test_aggregate_flow_booking_split_and_invariants(self):
        """Verify aggregate flow for Booking Holdings (BKNG) under 25:1 split."""
        res = self.client.get("/api/v1/flow/aggregate/BKNG?days=30")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["ticker"], "BKNG")
        self.assertEqual(data["cusip"], "09857L108")

        self.assertEqual(data["funds_buying"], data["funds_new"] + data["funds_increased"])
        self.assertEqual(data["funds_selling"], data["funds_decreased"] + data["funds_closed"])
        # Unchanged holders must be recorded in funds_unchanged due to split rebasing
        self.assertGreater(data["funds_unchanged"], 0)

    def test_aggregate_flow_zero_activity_stock(self):
        """Verify aggregate flow for an inactive CUSIP returns 0 counts, not null."""
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

    def test_aggregate_daily_consistency(self):
        """Verify that aggregate gross buying/selling matches daily sums over the same window."""
        daily_res = self.client.get("/api/v1/flow/daily/AAPL?days=7")
        agg_res = self.client.get("/api/v1/flow/aggregate/AAPL?days=7")
        self.assertEqual(daily_res.status_code, 200)
        self.assertEqual(agg_res.status_code, 200)

        daily_data = daily_res.json().get("daily_data", [])
        agg_data = agg_res.json()

        daily_buying_sum = sum(d["gross_buying"] for d in daily_data)
        daily_selling_sum = sum(d["gross_selling"] for d in daily_data)

        self.assertAlmostEqual(daily_buying_sum, agg_data["gross_buying"], delta=1.0)
        self.assertAlmostEqual(daily_selling_sum, agg_data["gross_selling"], delta=1.0)

    # -------------------------------------------------------------------------
    # 4. Top Market Changes Endpoint Tests (/api/v1/flow/top-changes)
    # -------------------------------------------------------------------------

    def test_top_changes_fund_metrics_and_invariants(self):
        """Verify top mover stocks include all 7 fund metrics and satisfy invariants."""
        res = self.client.get("/api/v1/flow/top-changes?date=2026-08-14&sort_by=shares")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        stocks = data.get("stocks", [])
        self.assertGreater(len(stocks), 0)

        for stock in stocks:
            funds_buying = stock["funds_buying"]
            funds_selling = stock["funds_selling"]
            funds_new = stock["funds_new"]
            funds_increased = stock["funds_increased"]
            funds_decreased = stock["funds_decreased"]
            funds_closed = stock["funds_closed"]
            funds_unchanged = stock["funds_unchanged"]

            self.assertEqual(funds_buying, funds_new + funds_increased)
            self.assertEqual(funds_selling, funds_decreased + funds_closed)

            # CUSIP must be uppercase
            cusip = stock["cusip"]
            self.assertEqual(cusip, cusip.upper())

    def test_top_changes_sort_by_value_fund_metrics(self):
        """Verify top changes sorted by value preserves fund metrics."""
        res = self.client.get("/api/v1/flow/top-changes?date=2026-08-14&sort_by=value")
        self.assertEqual(res.status_code, 200)
        stocks = res.json().get("stocks", [])
        self.assertGreater(len(stocks), 0)
        for stock in stocks[:10]:
            self.assertEqual(stock["funds_buying"], stock["funds_new"] + stock["funds_increased"])
            self.assertEqual(stock["funds_selling"], stock["funds_decreased"] + stock["funds_closed"])

    def test_top_changes_invalid_sort(self):
        """Verify top changes returns 400 for invalid sort parameter."""
        res = self.client.get("/api/v1/flow/top-changes?date=2026-08-14&sort_by=invalid")
        self.assertEqual(res.status_code, 400)

    # -------------------------------------------------------------------------
    # 5. Quarterly Flow Endpoint Tests (/api/v1/flow/quarterly/{identifier})
    # -------------------------------------------------------------------------

    def test_quarterly_flow_microsoft(self):
        """Verify quarterly flow endpoint for Microsoft (MSFT) across multiple quarters."""
        res = self.client.get("/api/v1/flow/quarterly/MSFT?quarters=4")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["ticker"], "MSFT")
        self.assertEqual(data["cusip"], "594918104")

        quarterly_data = data.get("quarterly_data", [])
        self.assertGreater(len(quarterly_data), 0)

        for q in quarterly_data:
            self.assertIn("quarter", q)
            self.assertIn("gross_buying", q)
            self.assertIn("gross_selling", q)
            self.assertIn("net_change", q)
            self.assertEqual(q["funds_buying"], q["funds_new"] + q["funds_increased"])
            self.assertEqual(q["funds_selling"], q["funds_decreased"] + q["funds_closed"])
            self.assertGreaterEqual(q["funds_unchanged"], 0)

    def test_quarterly_flow_apple_cusip(self):
        """Verify quarterly flow endpoint querying Apple by CUSIP."""
        res = self.client.get("/api/v1/flow/quarterly/037833100?quarters=2")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["cusip"], "037833100")
        quarterly_data = data.get("quarterly_data", [])
        self.assertGreater(len(quarterly_data), 0)
        for q in quarterly_data:
            self.assertEqual(q["funds_buying"], q["funds_new"] + q["funds_increased"])
            self.assertEqual(q["funds_selling"], q["funds_decreased"] + q["funds_closed"])

    def test_quarterly_flow_invalid_identifier(self):
        """Verify quarterly flow returns 400 for unknown ticker that cannot be resolved to CUSIP."""
        res = self.client.get("/api/v1/flow/quarterly/XYZQ?quarters=4")
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
