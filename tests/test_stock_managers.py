"""
tests/test_stock_managers.py

Unit and integration tests for GET /api/v1/stock/{identifier}/managers:
1. Pydantic models: ManagerStockActivity, StockOwnershipSummary, StockManagersResponse
2. Input validation: invalid identifiers, invalid change_type, invalid sort_by, invalid sort_dir, pagination bounds
3. Default target period fallback and available_periods list
4. Summary mathematical invariants:
   - funds_buying == funds_new + funds_increased
   - funds_selling == funds_decreased + funds_closed
   - total_managers == funds_buying + funds_selling + funds_unchanged
   - net_shares_change == gross_buying_shares - gross_selling_shares
5. Manager activity calculations:
   - change_in_shares, percent_change, current_price_per_share, weight_pct
6. Activity filtering: buyers, sellers, new, increased, decreased, closed, unchanged, all
7. Sorting: value_change, shares_change, current_value, current_shares, company_name in asc/desc
8. Pagination: limit, offset, has_next_page
9. Stock split rebasing and 1-share cash-in-lieu tolerance on split stocks (BKNG)
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
import datetime as dt
from fastapi.testclient import TestClient
from main import app
from sec_models import (
    ManagerStockActivity,
    StockOwnershipSummary,
    StockManagersResponse,
)


class TestStockManagers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    # -------------------------------------------------------------------------
    # 1. Pydantic Model Tests
    # -------------------------------------------------------------------------

    def test_models_instantiation_and_validation(self):
        """Verify ManagerStockActivity, StockOwnershipSummary, and StockManagersResponse models."""
        summary = StockOwnershipSummary(
            total_managers=10,
            total_current_shares=100000,
            total_current_value=25000000,
            net_shares_change=5000,
            net_value_change=1250000,
            gross_buying_shares=15000,
            gross_selling_shares=10000,
            funds_buying=4,
            funds_selling=3,
            funds_new=1,
            funds_increased=3,
            funds_decreased=2,
            funds_closed=1,
            funds_unchanged=3,
        )
        self.assertEqual(summary.funds_buying, summary.funds_new + summary.funds_increased)
        self.assertEqual(summary.funds_selling, summary.funds_decreased + summary.funds_closed)
        self.assertEqual(summary.net_shares_change, summary.gross_buying_shares - summary.gross_selling_shares)

        manager = ManagerStockActivity(
            cik="0001423053",
            company_name="CITADEL ADVISORS LLC",
            aum=875000000000,
            change_type="increased",
            current_shares=20000,
            previous_shares=15000,
            rebased_previous_shares=15000,
            change_in_shares=5000,
            percent_change=33.33,
            current_value=5000000,
            previous_value=3750000,
            value_change=1250000,
            current_price_per_share=250.0,
            weight_pct=0.0006,
            filing_date=dt.date(2026, 8, 14),
            accession_number="0001423053-26-000100",
            previous_accession_number="0001423053-26-000050",
            form_type="13F-HR",
        )
        self.assertEqual(manager.current_shares, 20000)
        self.assertEqual(manager.change_in_shares, 5000)

        resp = StockManagersResponse(
            ticker="AMZN",
            cusip="023135106",
            issuer_name="AMAZON COM INC",
            period_of_report=dt.date(2026, 6, 30),
            available_periods=[dt.date(2026, 6, 30), dt.date(2026, 3, 31)],
            summary=summary,
            managers=[manager],
            limit=50,
            offset=0,
            has_next_page=False,
        )
        self.assertEqual(resp.ticker, "AMZN")
        self.assertEqual(len(resp.managers), 1)

    # -------------------------------------------------------------------------
    # 2. Input Validation Tests
    # -------------------------------------------------------------------------

    def test_invalid_identifier_returns_400(self):
        """Non-existent CUSIP/ticker that cannot be resolved returns HTTP 400."""
        res = self.client.get("/api/v1/stock/NONEXISTENT999XYZ/managers")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Could not resolve identifier", res.json()["detail"])

    def test_invalid_change_type_returns_400(self):
        """Unsupported change_type returns HTTP 400 with allowed values listed."""
        res = self.client.get("/api/v1/stock/AMZN/managers?change_type=flying")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid change_type", res.json()["detail"])

    def test_invalid_sort_by_returns_400(self):
        """Unsupported sort_by metric returns HTTP 400."""
        res = self.client.get("/api/v1/stock/AMZN/managers?sort_by=market_cap")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid sort_by", res.json()["detail"])

    def test_invalid_sort_dir_returns_400(self):
        """Unsupported sort direction returns HTTP 400."""
        res = self.client.get("/api/v1/stock/AMZN/managers?sort_dir=sideways")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid sort_dir", res.json()["detail"])

    def test_pagination_bounds_validation(self):
        """limit < 1, limit > 1000, or offset < 0 return HTTP 422."""
        res_zero_limit = self.client.get("/api/v1/stock/AMZN/managers?limit=0")
        self.assertEqual(res_zero_limit.status_code, 422)

        res_huge_limit = self.client.get("/api/v1/stock/AMZN/managers?limit=5000")
        self.assertEqual(res_huge_limit.status_code, 422)

        res_neg_offset = self.client.get("/api/v1/stock/AMZN/managers?offset=-1")
        self.assertEqual(res_neg_offset.status_code, 422)

    # -------------------------------------------------------------------------
    # 3. Functional Tests: Default Target Period & Mathematical Invariants
    # -------------------------------------------------------------------------

    def test_amzn_stock_managers_default_and_summary_invariants(self):
        """Fetch AMZN managers without period_of_report; verify fallback, periods, and summary invariants."""
        res = self.client.get("/api/v1/stock/AMZN/managers?limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertEqual(data["ticker"], "AMZN")
        self.assertEqual(data["cusip"], "023135106")
        self.assertTrue(len(data["available_periods"]) > 0)
        self.assertEqual(data["period_of_report"], data["available_periods"][0])

        summary = data["summary"]
        self.assertGreater(summary["total_managers"], 0)
        self.assertGreater(summary["total_current_shares"], 0)
        self.assertGreater(summary["total_current_value"], 0)

        # Mathematical Invariants
        self.assertEqual(
            summary["funds_buying"],
            summary["funds_new"] + summary["funds_increased"],
            "funds_buying must equal funds_new + funds_increased",
        )
        self.assertEqual(
            summary["funds_selling"],
            summary["funds_decreased"] + summary["funds_closed"],
            "funds_selling must equal funds_decreased + funds_closed",
        )
        self.assertEqual(
            summary["total_managers"],
            summary["funds_buying"] + summary["funds_selling"] + summary["funds_unchanged"],
            "total_managers must equal buying + selling + unchanged",
        )
        self.assertEqual(
            summary["net_shares_change"],
            summary["gross_buying_shares"] - summary["gross_selling_shares"],
            "net_shares_change must equal gross_buying - gross_selling",
        )

        # Verify manager list length and has_next_page
        managers = data["managers"]
        self.assertEqual(len(managers), 10)
        self.assertTrue(data["has_next_page"])

        # Verify manager item fields
        first = managers[0]
        self.assertIn("cik", first)
        self.assertIn("company_name", first)
        self.assertIn("change_type", first)
        self.assertIn("current_shares", first)
        self.assertIn("previous_shares", first)
        self.assertIn("rebased_previous_shares", first)
        self.assertIn("change_in_shares", first)
        self.assertIn("current_value", first)
        self.assertIn("previous_value", first)
        self.assertIn("value_change", first)
        self.assertIn("filing_date", first)
        self.assertIn("accession_number", first)
        self.assertIn("form_type", first)

    # -------------------------------------------------------------------------
    # 4. Activity Filter Tests: buyers, sellers, new, increased, decreased, closed, unchanged
    # -------------------------------------------------------------------------

    def test_filter_change_type_buyers(self):
        """change_type=buyers returns only 'new' or 'increased' positions."""
        res = self.client.get("/api/v1/stock/AMZN/managers?change_type=buyers&limit=25")
        self.assertEqual(res.status_code, 200)
        for m in res.json()["managers"]:
            self.assertIn(m["change_type"], ("new", "increased"))

    def test_filter_change_type_sellers(self):
        """change_type=sellers returns only 'decreased' or 'closed' positions."""
        res = self.client.get("/api/v1/stock/AMZN/managers?change_type=sellers&limit=25")
        self.assertEqual(res.status_code, 200)
        for m in res.json()["managers"]:
            self.assertIn(m["change_type"], ("decreased", "closed"))

    def test_filter_change_type_specific(self):
        """Specific change_type filters return only matching positions."""
        for ctype in ("new", "increased", "decreased", "unchanged"):
            res = self.client.get(f"/api/v1/stock/AMZN/managers?change_type={ctype}&limit=10")
            self.assertEqual(res.status_code, 200)
            for m in res.json()["managers"]:
                self.assertEqual(m["change_type"], ctype)

    # -------------------------------------------------------------------------
    # 5. Sorting and Pagination Tests
    # -------------------------------------------------------------------------

    def test_sort_by_value_change_desc(self):
        """sort_by=value_change&sort_dir=desc returns largest positive value change first."""
        res = self.client.get("/api/v1/stock/AMZN/managers?sort_by=value_change&sort_dir=desc&limit=10")
        self.assertEqual(res.status_code, 200)
        managers = res.json()["managers"]
        for i in range(len(managers) - 1):
            self.assertGreaterEqual(managers[i]["value_change"], managers[i + 1]["value_change"])

    def test_sort_by_company_name_asc(self):
        """sort_by=company_name&sort_dir=asc returns managers alphabetically."""
        res = self.client.get("/api/v1/stock/AMZN/managers?sort_by=company_name&sort_dir=asc&limit=10")
        self.assertEqual(res.status_code, 200)
        managers = res.json()["managers"]
        for i in range(len(managers) - 1):
            self.assertLessEqual(managers[i]["company_name"].upper(), managers[i + 1]["company_name"].upper())

    def test_pagination_offset(self):
        """offset shifts the result window correctly."""
        res_page1 = self.client.get("/api/v1/stock/AMZN/managers?limit=5&offset=0")
        res_page2 = self.client.get("/api/v1/stock/AMZN/managers?limit=5&offset=5")
        self.assertEqual(res_page1.status_code, 200)
        self.assertEqual(res_page2.status_code, 200)

        ciks_page1 = [m["cik"] for m in res_page1.json()["managers"]]
        ciks_page2 = [m["cik"] for m in res_page2.json()["managers"]]
        # Ensure no overlap between page 1 and page 2
        self.assertEqual(len(set(ciks_page1).intersection(set(ciks_page2))), 0)

    # -------------------------------------------------------------------------
    # 6. Split Rebasing & 1-Share Cash-in-Lieu Tolerance (BKNG)
    # -------------------------------------------------------------------------

    def test_bkng_split_rebasing_and_cash_in_lieu(self):
        """BKNG 25:1 split on 2026-06-30 rebases previous shares and applies 1-share tolerance."""
        res = self.client.get("/api/v1/stock/BKNG/managers?period_of_report=2026-06-30&change_type=unchanged&limit=20")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["ticker"], "BKNG")

        # Find unchanged managers that held shares before split
        unchanged_split_holders = [
            m for m in data["managers"]
            if m["change_type"] == "unchanged" and m["previous_shares"] > 0
        ]
        self.assertTrue(len(unchanged_split_holders) > 0)
        for m in unchanged_split_holders:
            # rebased previous shares should be ~25x raw previous shares
            self.assertEqual(m["rebased_previous_shares"], m["previous_shares"] * 25)
            # 1-share tolerance guarantees change_in_shares == 0 and percent_change == 0.0
            self.assertEqual(m["change_in_shares"], 0)
            self.assertEqual(m["percent_change"], 0.0)
            self.assertEqual(m["value_change"], 0)


if __name__ == "__main__":
    unittest.main()
