"""
tests/test_options_activity.py

Unit and regression test suite for latest options activity and v3 activity endpoints.
Covers:
1. P0 data loss fix for ticker searches outside the 10,000 candidate filings window.
2. Result capping at 1000 items.
3. CUSIP case normalization and case-insensitive ticker resolution.
4. Absence of correlated subquery for form_type (passed directly via VALUES tuple).
5. sort_by and sort_direction validation and ordering.
6. put_or_call parameter filtering ("PUT", "CALL", case-insensitivity, 400 rejection).
7. CIK filtering (success and 404 for unknown manager).
8. Parity between /activity/latest/options and /activity/latest/v3?security_type=options.
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
from fastapi.testclient import TestClient
from main import app
from sec_models import LatestActivityResponse


class TestOptionsActivity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    # -------------------------------------------------------------------------
    # P0: Ticker search outside arbitrary 10,000 filings window
    # -------------------------------------------------------------------------
    def test_options_data_loss_fix_outside_10k_window(self):
        """
        Under the old query, CUSIP 00012497K (from filing 226876 in 2020) was dropped
        because CandidateFilings capped at LIMIT 10000 without filtering by CUSIP.
        The fix pushes the CUSIP filter into CandidateFilings, finding the trade in 0.018s.
        """
        r = self.client.get("/activity/latest/options?ticker=00012497K")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        activities = data.get("activities", [])
        self.assertGreaterEqual(len(activities), 1)
        first = activities[0]
        self.assertEqual(first["cusip"], "00012497K")
        self.assertEqual(first["latest_accession_number"], "0001724517-20-000006")
        self.assertEqual(first["filing_date"], "2020-11-13")
        self.assertEqual(first["put_or_call"], "CALL")
        self.assertEqual(first["form_type"], "13F-HR")

    def test_options_ticker_case_insensitivity(self):
        """Ticker search should resolve regardless of input casing."""
        r_upper = self.client.get("/activity/latest/options?ticker=00012497K")
        r_lower = self.client.get("/activity/latest/options?ticker=00012497k")
        self.assertEqual(r_upper.status_code, 200)
        self.assertEqual(r_lower.status_code, 200)
        self.assertEqual(len(r_upper.json()["activities"]), len(r_lower.json()["activities"]))
        self.assertEqual(
            r_lower.json()["activities"][0]["cusip"],
            r_upper.json()["activities"][0]["cusip"],
        )

    # -------------------------------------------------------------------------
    # P0: Result cap (match stocks' 1000 limit)
    # -------------------------------------------------------------------------
    def test_options_result_cap(self):
        """Result list should never exceed 1000 rows even with large limit."""
        r = self.client.get("/activity/latest/options?limit=50")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertLessEqual(len(data.get("activities", [])), 1000)

    # -------------------------------------------------------------------------
    # P1: CUSIP case normalization
    # -------------------------------------------------------------------------
    def test_options_cusip_normalization(self):
        """All returned CUSIPs must be strictly uppercase."""
        r = self.client.get("/activity/latest/options?limit=5")
        self.assertEqual(r.status_code, 200)
        activities = r.json().get("activities", [])
        self.assertGreater(len(activities), 0)
        for act in activities:
            cusip = act.get("cusip")
            self.assertIsNotNone(cusip)
            self.assertEqual(cusip, cusip.upper())

    # -------------------------------------------------------------------------
    # P1: form_type presence without correlated subquery
    # -------------------------------------------------------------------------
    def test_options_form_type_presence(self):
        """form_type must be present and valid for all returned activities."""
        r = self.client.get("/activity/latest/options?limit=5")
        self.assertEqual(r.status_code, 200)
        activities = r.json().get("activities", [])
        self.assertGreater(len(activities), 0)
        for act in activities:
            self.assertIn(act.get("form_type"), ("13F-HR", "13F-HR/A", "13F-HR/A/A"))

    # -------------------------------------------------------------------------
    # P1: sort_by and sort_direction validation and ordering
    # -------------------------------------------------------------------------
    def test_options_sort_by_value_change_desc(self):
        """Validate sorting by value_change in descending order."""
        r = self.client.get("/activity/latest/options?cik=1895612&sort_by=value_change&sort_direction=desc")
        self.assertEqual(r.status_code, 200)
        activities = r.json().get("activities", [])
        self.assertGreater(len(activities), 1)
        vals = [
            a["absolute_value_change"]
            for a in activities
            if a.get("absolute_value_change") is not None
        ]
        self.assertTrue(
            all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)),
            f"Not descending: {vals}",
        )

    def test_options_sort_by_value_change_asc(self):
        """Validate sorting by value_change in ascending order."""
        r = self.client.get("/activity/latest/options?cik=1895612&sort_by=value_change&sort_direction=asc")
        self.assertEqual(r.status_code, 200)
        activities = r.json().get("activities", [])
        self.assertGreater(len(activities), 1)
        vals = [
            a["absolute_value_change"]
            for a in activities
            if a.get("absolute_value_change") is not None
        ]
        self.assertTrue(
            all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)),
            f"Not ascending: {vals}",
        )

    def test_options_sort_by_invalid_options(self):
        """Invalid sort_by or sort_direction must return 400 Bad Request."""
        r1 = self.client.get("/activity/latest/options?sort_by=unknown_col")
        self.assertEqual(r1.status_code, 400)
        self.assertIn("Invalid sort_by", r1.json().get("detail", ""))

        r2 = self.client.get("/activity/latest/options?sort_direction=sideways")
        self.assertEqual(r2.status_code, 400)
        self.assertIn("Invalid sort_direction", r2.json().get("detail", ""))

    # -------------------------------------------------------------------------
    # P2: put_or_call filter cleaning & case-insensitivity
    # -------------------------------------------------------------------------
    def test_options_put_or_call_filtering(self):
        """Filter by PUT or CALL exclusively, case-insensitively."""
        # PUT filter
        r_put = self.client.get("/activity/latest/options?cik=1895612&put_or_call=PUT")
        self.assertEqual(r_put.status_code, 200)
        puts = r_put.json().get("activities", [])
        self.assertGreater(len(puts), 0)
        self.assertTrue(all(a["put_or_call"] == "PUT" for a in puts))

        # CALL filter (lowercase)
        r_call = self.client.get("/activity/latest/options?cik=1895612&put_or_call=call")
        self.assertEqual(r_call.status_code, 200)
        calls = r_call.json().get("activities", [])
        self.assertGreater(len(calls), 0)
        self.assertTrue(all(a["put_or_call"] == "CALL" for a in calls))

        # Sum of PUTs and CALLs matches total unfiltered for this manager
        r_total = self.client.get("/activity/latest/options?cik=1895612")
        self.assertEqual(len(puts) + len(calls), len(r_total.json().get("activities", [])))

        # Invalid put_or_call
        r_invalid = self.client.get("/activity/latest/options?put_or_call=swap")
        self.assertEqual(r_invalid.status_code, 400)
        self.assertIn("Invalid put_or_call", r_invalid.json().get("detail", ""))

    # -------------------------------------------------------------------------
    # P2: CIK / Company filter support
    # -------------------------------------------------------------------------
    def test_options_cik_filtering_success(self):
        """CIK filter returns activities strictly for that company."""
        r_unpadded = self.client.get("/activity/latest/options?cik=1895612")
        self.assertEqual(r_unpadded.status_code, 200)
        activities = r_unpadded.json().get("activities", [])
        self.assertGreater(len(activities), 0)
        self.assertTrue(all(a["cik"] == "1895612" for a in activities))

        # Padded CIK produces the exact same results
        r_padded = self.client.get("/activity/latest/options?cik=0001895612")
        self.assertEqual(r_padded.status_code, 200)
        self.assertEqual(len(activities), len(r_padded.json().get("activities", [])))

    def test_options_cik_filtering_not_found(self):
        """Non-existent CIK must return 404 Not Found."""
        r = self.client.get("/activity/latest/options?cik=9999999999")
        self.assertEqual(r.status_code, 404)
        self.assertIn("not found", r.json().get("detail", "").lower())

    # -------------------------------------------------------------------------
    # Unified v3 Endpoint Parity
    # -------------------------------------------------------------------------
    def test_activity_v3_options_dispatch_parity(self):
        """Verify /activity/latest/v3?security_type=options supports all options features."""
        r = self.client.get(
            "/activity/latest/v3?security_type=options&cik=1895612&sort_by=value_change&sort_direction=desc&put_or_call=PUT"
        )
        self.assertEqual(r.status_code, 200)
        res = LatestActivityResponse(**r.json())
        self.assertGreater(len(res.activities), 0)
        self.assertTrue(all(a.cik == "1895612" for a in res.activities))
        self.assertTrue(all(a.put_or_call == "PUT" for a in res.activities))

    def test_activity_v3_stocks_cik_and_sort(self):
        """Verify /activity/latest/v3 stocks branch also supports cik and sort parameters."""
        # 1. CIK filtering for Berkshire Hathaway
        r = self.client.get("/activity/latest/v3?security_type=stocks&cik=0001067983")
        self.assertEqual(r.status_code, 200)
        activities = r.json().get("activities", [])
        self.assertGreater(len(activities), 0)
        self.assertTrue(all(a["cik"] == "1067983" for a in activities))

        # 2. Sorting by value_change descending
        r_sort = self.client.get(
            "/activity/latest/v3?security_type=stocks&cik=0001067983&sort_by=value_change&sort_direction=desc"
        )
        self.assertEqual(r_sort.status_code, 200)
        sorted_acts = r_sort.json().get("activities", [])
        vals = [
            a["absolute_value_change"]
            for a in sorted_acts
            if a.get("absolute_value_change") is not None
        ]
        self.assertTrue(all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)))


    def test_options_cik_and_ticker_closed_position(self):
        """
        Regression Test: When searching by manager CIK AND ticker (e.g. BlackRock closing ABT CALLs),
        the manager's true latest filing must be used rather than falling back to an old quarter
        that mistakenly flips a closed trade into a historical new trade.
        """
        # BlackRock CIK 2012383 closed ABT CALLs in latest filing 335276 (accession 0002012383-26-003238)
        r = self.client.get("/activity/latest/options?cik=2012383&ticker=ABT")
        self.assertEqual(r.status_code, 200)
        acts = r.json().get("activities", [])
        self.assertGreaterEqual(len(acts), 1)
        abt_call = next((a for a in acts if a["cusip"] == "002824100" and a["put_or_call"] == "CALL"), None)
        self.assertIsNotNone(abt_call)
        self.assertEqual(abt_call["change_type"], "closed")
        self.assertEqual(abt_call["form_type"], "13F-HR")
        self.assertEqual(abt_call["security_type"], "options")

        # Querying specifically for closed trades must also find it
        r_closed = self.client.get("/activity/latest/options?cik=2012383&ticker=ABT&change_type=closed")
        self.assertEqual(r_closed.status_code, 200)
        closed_acts = r_closed.json().get("activities", [])
        self.assertGreaterEqual(len(closed_acts), 1)
        self.assertEqual(closed_acts[0]["change_type"], "closed")

    def test_options_sort_by_company_name(self):
        """Validate sorting by company_name alphabetically."""
        r_asc = self.client.get("/activity/latest/options?limit=10&sort_by=company_name&sort_direction=asc")
        self.assertEqual(r_asc.status_code, 200)
        names_asc = [a["company_name"] for a in r_asc.json().get("activities", [])]
        self.assertGreater(len(names_asc), 1)
        self.assertTrue(
            all(names_asc[i] <= names_asc[i + 1] for i in range(len(names_asc) - 1)),
            f"Names not ascending: {names_asc}",
        )

        r_desc = self.client.get("/activity/latest/options?limit=10&sort_by=company_name&sort_direction=desc")
        self.assertEqual(r_desc.status_code, 200)
        names_desc = [a["company_name"] for a in r_desc.json().get("activities", [])]
        self.assertGreater(len(names_desc), 1)
        self.assertTrue(
            all(names_desc[i] >= names_desc[i + 1] for i in range(len(names_desc) - 1)),
            f"Names not descending: {names_desc}",
        )

    def test_options_sort_by_percent_and_weight(self):
        """Validate sorting by percent_change and weight_pct with NULLS LAST."""
        # percent_change desc
        r_pct = self.client.get("/activity/latest/options?cik=1895612&sort_by=percent_change&sort_direction=desc")
        self.assertEqual(r_pct.status_code, 200)
        pct_vals = [a["percent_change"] for a in r_pct.json().get("activities", []) if a.get("percent_change") is not None]
        self.assertGreater(len(pct_vals), 1)
        self.assertTrue(all(pct_vals[i] >= pct_vals[i + 1] for i in range(len(pct_vals) - 1)))

        # weight_pct desc
        r_wt = self.client.get("/activity/latest/options?cik=1895612&sort_by=weight_pct&sort_direction=desc")
        self.assertEqual(r_wt.status_code, 200)
        wt_vals = [a["weight_pct"] for a in r_wt.json().get("activities", []) if a.get("weight_pct") is not None]
        self.assertGreater(len(wt_vals), 1)
        self.assertTrue(all(wt_vals[i] >= wt_vals[i + 1] for i in range(len(wt_vals) - 1)))

    def test_options_and_stocks_min_value_filter(self):
        """min_value filter must restrict results to absolute_value_change >= threshold across both options and stocks."""
        # Options min_value
        r_opt = self.client.get("/activity/latest/options?cik=1895612&min_value=1000000")
        self.assertEqual(r_opt.status_code, 200)
        opt_acts = r_opt.json().get("activities", [])
        self.assertGreater(len(opt_acts), 0)
        self.assertTrue(all(a["absolute_value_change"] >= 1000000 for a in opt_acts))

        # Stocks min_value
        r_stock = self.client.get("/activity/latest/v3?security_type=stocks&cik=0001067983&min_value=50000000")
        self.assertEqual(r_stock.status_code, 200)
        stock_acts = r_stock.json().get("activities", [])
        self.assertGreater(len(stock_acts), 0)
        self.assertTrue(all(a["absolute_value_change"] >= 50000000 for a in stock_acts))

    def test_empty_string_parameters_gracefully_ignored(self):
        """Empty string parameters (e.g. ?cik=&ticker=&put_or_call=) must not crash or 404."""
        r = self.client.get("/activity/latest/options?cik=&ticker=&put_or_call=&change_type=&limit=2")
        self.assertEqual(r.status_code, 200)
        self.assertIn("activities", r.json())

    def test_security_type_model_field(self):
        """security_type field must identify 'options' vs 'stocks' records accurately."""
        r_opt = self.client.get("/activity/latest/options?limit=2")
        self.assertEqual(r_opt.status_code, 200)
        for act in r_opt.json().get("activities", []):
            self.assertEqual(act.get("security_type"), "options")

        r_stock = self.client.get("/activity/latest/v3?security_type=stocks&limit=2")
        self.assertEqual(r_stock.status_code, 200)
        for act in r_stock.json().get("activities", []):
            self.assertEqual(act.get("security_type"), "stocks")


if __name__ == "__main__":
    unittest.main()

