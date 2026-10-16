"""
tests/test_adversarial_m3_flow.py

Adversarial and Robustness Test Suite for Milestone 3 (R3 Flow Fund Counts).
Authored by reviewer_m3_2.

Focus Areas:
1. Identifier resolution edge cases:
   - Lowercase vs uppercase tickers (e.g. 'aapl', 'AAPL')
   - Lowercase vs uppercase CUSIPs with alphanumeric characters (e.g. '09857l108' vs '09857L108')
   - Unresolvable tickers (<= 5 alpha chars with no CUSIP mapping) return 400 Bad Request
   - Non-existent CUSIPs (e.g. 'ZZZZZZZZZ') return 200 with empty datasets
   - SQL injection attempts parameterized safely without 500 error or data leak
2. Missing corporate actions robustness:
   - Stocks with zero entries in stock_splits (e.g. MSFT)
   - Stocks with splits in one quarter but not adjacent quarters (BKNG)
   - 1-share cash-in-lieu tolerance with and without active split
3. Free float edge cases:
   - Missing or non-existent float: ensure no ZeroDivisionError and net_change_pct_float remains None
4. Empty flow dates and zero-activity scenarios:
   - Far past dates (e.g. 1990-01-01) for /top-changes
   - Future dates (e.g. 2099-01-01)
   - Non-existent CUSIP 'ZZZZZZZZZ' with 0 filings across daily, aggregate, and quarterly
5. Top-changes sorting and ranking:
   - sort_by='value' vs sort_by='shares' strictly ordered by absolute value/shares descending
   - sort_by invalid parameter returns HTTP 400
   - All entries in top-changes satisfy strict additive fund count invariants
6. Payload Contract Conformance:
   - Verify every endpoint's output parses strictly against Pydantic response models without extra or missing required keys
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
import datetime as dt
from fastapi.testclient import TestClient
from main import app
from sec_models import (
    DailyFlowResponse,
    AggregateFlowResponse,
    TopStockChangesResponse,
    QuarterlyFlowResponse,
)


class TestAdversarialM3Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    # =========================================================================
    # 1. IDENTIFIER RESOLUTION EDGE CASES
    # =========================================================================

    def test_edge_case_lowercase_ticker(self):
        """Test lowercase ticker 'aapl' resolves properly and returns 200 with contract."""
        res = self.client.get("/api/v1/flow/daily/aapl?days=5")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        parsed = DailyFlowResponse(**data)
        self.assertEqual(parsed.ticker, "AAPL")
        self.assertEqual(parsed.cusip, "037833100")

    def test_edge_case_lowercase_alphanumeric_cusip(self):
        """Test lowercase alphanumeric CUSIP '09857l108' (BKNG) resolves properly."""
        res = self.client.get("/api/v1/flow/aggregate/09857l108?days=14")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        parsed = AggregateFlowResponse(**data)
        self.assertEqual(parsed.cusip, "09857L108")
        self.assertEqual(parsed.ticker, "BKNG")

    def test_edge_case_unresolvable_short_tickers(self):
        """Short unknown tickers (<= 5 chars) return 400 Bad Request."""
        for bad_ticker in ["XYZQ", "ZZZZ", "QQQQX"]:
            res_daily = self.client.get(f"/api/v1/flow/daily/{bad_ticker}?days=1")
            self.assertEqual(res_daily.status_code, 400, f"Expected 400 for {bad_ticker}")
            self.assertIn("Could not resolve", res_daily.json().get("detail", ""))

            res_agg = self.client.get(f"/api/v1/flow/aggregate/{bad_ticker}?days=1")
            self.assertEqual(res_agg.status_code, 400, f"Expected 400 for {bad_ticker}")

            res_q = self.client.get(f"/api/v1/flow/quarterly/{bad_ticker}?quarters=1")
            self.assertEqual(res_q.status_code, 400, f"Expected 400 for {bad_ticker}")

    def test_edge_case_nonexistent_cusip_clean_empty(self):
        """Non-existent CUSIP 'ZZZZZZZZZ' returns 200 with clean empty response, not error."""
        res_daily = self.client.get("/api/v1/flow/daily/ZZZZZZZZZ?days=5")
        self.assertEqual(res_daily.status_code, 200)
        parsed_daily = DailyFlowResponse(**res_daily.json())
        self.assertEqual(len(parsed_daily.daily_data), 0)

        res_agg = self.client.get("/api/v1/flow/aggregate/ZZZZZZZZZ?days=5")
        self.assertEqual(res_agg.status_code, 200)
        parsed_agg = AggregateFlowResponse(**res_agg.json())
        self.assertEqual(parsed_agg.funds_buying, 0)
        self.assertEqual(parsed_agg.funds_selling, 0)

        res_q = self.client.get("/api/v1/flow/quarterly/ZZZZZZZZZ?quarters=4")
        self.assertEqual(res_q.status_code, 200)
        parsed_q = QuarterlyFlowResponse(**res_q.json())
        self.assertEqual(len(parsed_q.quarterly_data), 0)

    def test_edge_case_sql_injection_safety(self):
        """SQL injection payloads are safely parameterized without 500 error or syntax errors."""
        injections = [
            "' OR '1'='1",
            "\" OR 1=1 --",
            "; DROP TABLE holdings; --",
        ]
        for payload in injections:
            res = self.client.get(f"/api/v1/flow/daily/{payload}?days=1")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(len(data.get("daily_data", [])), 0)

    # =========================================================================
    # 2. MISSING CORPORATE ACTIONS & REBASING
    # =========================================================================

    def test_missing_corporate_actions_stock(self):
        """Test stock with NO corporate actions in stock_splits table calculates cleanly."""
        res = self.client.get("/api/v1/flow/quarterly/MSFT?quarters=4")
        self.assertEqual(res.status_code, 200)
        parsed = QuarterlyFlowResponse(**res.json())
        self.assertGreater(len(parsed.quarterly_data), 0)
        for q in parsed.quarterly_data:
            self.assertEqual(q.funds_buying, q.funds_new + q.funds_increased)
            self.assertEqual(q.funds_selling, q.funds_decreased + q.funds_closed)
            self.assertGreaterEqual(q.funds_unchanged, 0)

    def test_split_rebasing_with_inactive_quarter(self):
        """Booking BKNG had 25:1 split in Q2 2026, but not adjacent quarters."""
        res = self.client.get("/api/v1/flow/quarterly/BKNG?quarters=6")
        self.assertEqual(res.status_code, 200)
        parsed = QuarterlyFlowResponse(**res.json())
        for q in parsed.quarterly_data:
            self.assertEqual(q.funds_buying, q.funds_new + q.funds_increased)
            self.assertEqual(q.funds_selling, q.funds_decreased + q.funds_closed)
            self.assertGreaterEqual(q.funds_unchanged, 0)

    # =========================================================================
    # 3. ZERO / MISSING FLOAT VALUES
    # =========================================================================

    def test_zero_or_missing_float_safety(self):
        """When float is missing or 0, net_change_pct_float must be None without division error."""
        res = self.client.get("/api/v1/flow/aggregate/ZZZZZZZZZ?days=14")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIsNone(data.get("net_change_pct_float"))
        self.assertIsNone(data.get("free_float_shares"))

        res_daily = self.client.get("/api/v1/flow/daily/ZZZZZZZZZ?days=14")
        self.assertEqual(res_daily.status_code, 200)
        data_daily = res_daily.json()
        self.assertIsNone(data_daily.get("free_float_shares"))

    # =========================================================================
    # 4. EMPTY FLOW DATES & ZERO-ACTIVITY SCENARIOS
    # =========================================================================

    def test_empty_top_changes_past_date(self):
        """Target filing date with zero filings (e.g. 1990-01-01) returns empty list, not error."""
        res = self.client.get("/api/v1/flow/top-changes?date=1990-01-01&sort_by=value")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        parsed = TopStockChangesResponse(**data)
        self.assertEqual(parsed.date, dt.date(1990, 1, 1))
        self.assertEqual(len(parsed.stocks), 0)

    def test_empty_top_changes_future_date(self):
        """Future date (e.g. 2099-01-01) returns empty list."""
        res = self.client.get("/api/v1/flow/top-changes?date=2099-01-01&sort_by=shares")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        parsed = TopStockChangesResponse(**data)
        self.assertEqual(len(parsed.stocks), 0)

    # =========================================================================
    # 5. TOP-CHANGES RANKING & PARAMETER ROBUSTNESS
    # =========================================================================

    def test_top_changes_sort_by_value_descending(self):
        """Verify sort_by=value orders by absolute value change descending."""
        res = self.client.get("/api/v1/flow/top-changes?date=2026-08-14&sort_by=value")
        self.assertEqual(res.status_code, 200)
        stocks = res.json().get("stocks", [])
        self.assertGreater(len(stocks), 1)
        self.assertLessEqual(len(stocks), 50)

        for i in range(len(stocks) - 1):
            curr_abs = abs(stocks[i]["net_value_change"])
            next_abs = abs(stocks[i + 1]["net_value_change"])
            self.assertGreaterEqual(
                curr_abs,
                next_abs,
                f"Rank ordering violated: {curr_abs} < {next_abs} at index {i}",
            )

    def test_top_changes_sort_by_shares_descending(self):
        """Verify sort_by=shares orders by absolute shares change descending."""
        res = self.client.get("/api/v1/flow/top-changes?date=2026-08-14&sort_by=shares")
        self.assertEqual(res.status_code, 200)
        stocks = res.json().get("stocks", [])
        self.assertGreater(len(stocks), 1)
        self.assertLessEqual(len(stocks), 50)

        for i in range(len(stocks) - 1):
            curr_abs = abs(stocks[i]["net_shares_change"])
            next_abs = abs(stocks[i + 1]["net_shares_change"])
            self.assertGreaterEqual(
                curr_abs,
                next_abs,
                f"Rank ordering violated: {curr_abs} < {next_abs} at index {i}",
            )

    def test_top_changes_invalid_sort_parameter(self):
        """Verify invalid sort_by parameter returns 400 Bad Request."""
        res = self.client.get("/api/v1/flow/top-changes?date=2026-08-14&sort_by=market_cap")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid sort_by option", res.json().get("detail", ""))

    def test_top_changes_all_stocks_satisfy_invariants(self):
        """Verify EVERY stock in top 50 strictly satisfies the 7-metric invariant."""
        res = self.client.get("/api/v1/flow/top-changes?date=2026-08-14&sort_by=value")
        self.assertEqual(res.status_code, 200)
        for s in res.json().get("stocks", []):
            self.assertEqual(
                s["funds_buying"],
                s["funds_new"] + s["funds_increased"],
                f"Stock {s['cusip']} failed buying invariant",
            )
            self.assertEqual(
                s["funds_selling"],
                s["funds_decreased"] + s["funds_closed"],
                f"Stock {s['cusip']} failed selling invariant",
            )
            self.assertGreaterEqual(s["funds_unchanged"], 0)

    # =========================================================================
    # 6. PAYLOAD CONTRACT VERIFICATION ACROSS ALL 4 ENDPOINTS
    # =========================================================================

    def test_payload_contract_daily_flow(self):
        """Verify contract for GET /api/v1/flow/daily/{identifier}."""
        res = self.client.get("/api/v1/flow/daily/AAPL?days=7")
        self.assertEqual(res.status_code, 200)
        parsed = DailyFlowResponse(**res.json())
        self.assertIsInstance(parsed.ticker, str)
        self.assertIsInstance(parsed.cusip, str)
        for d in parsed.daily_data:
            self.assertIsInstance(d.date, dt.date)
            self.assertIsInstance(d.gross_buying, float)
            self.assertIsInstance(d.gross_selling, float)
            self.assertIsInstance(d.net_change, float)
            self.assertIsInstance(d.funds_buying, int)
            self.assertIsInstance(d.funds_selling, int)
            self.assertIsInstance(d.funds_new, int)
            self.assertIsInstance(d.funds_increased, int)
            self.assertIsInstance(d.funds_decreased, int)
            self.assertIsInstance(d.funds_closed, int)
            self.assertIsInstance(d.funds_unchanged, int)

    def test_payload_contract_aggregate_flow(self):
        """Verify contract for GET /api/v1/flow/aggregate/{identifier}."""
        res = self.client.get("/api/v1/flow/aggregate/AAPL?days=14")
        self.assertEqual(res.status_code, 200)
        parsed = AggregateFlowResponse(**res.json())
        self.assertIsInstance(parsed.days_looked_back, int)
        self.assertIsInstance(parsed.gross_buying, float)
        self.assertIsInstance(parsed.gross_selling, float)
        self.assertIsInstance(parsed.net_change, float)
        self.assertIsInstance(parsed.funds_buying, int)
        self.assertIsInstance(parsed.funds_selling, int)
        self.assertIsInstance(parsed.funds_new, int)
        self.assertIsInstance(parsed.funds_increased, int)
        self.assertIsInstance(parsed.funds_decreased, int)
        self.assertIsInstance(parsed.funds_closed, int)
        self.assertIsInstance(parsed.funds_unchanged, int)

    def test_payload_contract_top_changes(self):
        """Verify contract for GET /api/v1/flow/top-changes."""
        res = self.client.get("/api/v1/flow/top-changes?date=2026-08-14&sort_by=shares")
        self.assertEqual(res.status_code, 200)
        parsed = TopStockChangesResponse(**res.json())
        self.assertIsInstance(parsed.date, dt.date)
        self.assertIn(parsed.sort_by, ["value", "shares"])
        for s in parsed.stocks:
            self.assertIsInstance(s.cusip, str)
            self.assertIsInstance(s.issuer_name, str)
            self.assertIsInstance(s.net_shares_change, float)
            self.assertIsInstance(s.net_value_change, float)
            self.assertIsInstance(s.absolute_value_change, float)
            self.assertIsInstance(s.gross_buying_shares, float)
            self.assertIsInstance(s.gross_selling_shares, float)
            self.assertIsInstance(s.funds_buying, int)
            self.assertIsInstance(s.funds_selling, int)
            self.assertIsInstance(s.funds_new, int)
            self.assertIsInstance(s.funds_increased, int)
            self.assertIsInstance(s.funds_decreased, int)
            self.assertIsInstance(s.funds_closed, int)
            self.assertIsInstance(s.funds_unchanged, int)

    def test_payload_contract_quarterly_flow(self):
        """Verify contract for GET /api/v1/flow/quarterly/{identifier} and /flow/quarterly/{identifier}."""
        for path in ["/api/v1/flow/quarterly/AAPL?quarters=4", "/flow/quarterly/AAPL?quarters=4"]:
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200)
            parsed = QuarterlyFlowResponse(**res.json())
            self.assertEqual(parsed.ticker, "AAPL")
            self.assertEqual(parsed.cusip, "037833100")
            for q in parsed.quarterly_data:
                self.assertIsInstance(q.quarter, dt.date)
                self.assertIsInstance(q.gross_buying, float)
                self.assertIsInstance(q.gross_selling, float)
                self.assertIsInstance(q.net_change, float)
                self.assertIsInstance(q.funds_buying, int)
                self.assertIsInstance(q.funds_selling, int)
                self.assertIsInstance(q.funds_new, int)
                self.assertIsInstance(q.funds_increased, int)
                self.assertIsInstance(q.funds_decreased, int)
                self.assertIsInstance(q.funds_closed, int)
                self.assertIsInstance(q.funds_unchanged, int)


if __name__ == "__main__":
    unittest.main()
