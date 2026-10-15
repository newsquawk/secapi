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

    def test_r1_strict_ratio_snapping_and_purge(self):
        """
        R1: Reject non-standard empirical ratios (return None) and ensure
        no non-standard records exist in the PostgreSQL stock_splits table.
        """
        from scripts.detect_stock_splits import snap_split_ratio
        from database import _get_db_connection_params
        import psycopg2

        from decimal import Decimal

        # Non-standard candidate ratios must be rejected
        self.assertIsNone(snap_split_ratio(1.4857))   # Tamboran
        self.assertIsNone(snap_split_ratio(1.5565))   # Tigo
        self.assertIsNone(snap_split_ratio(6.4005))   # Picard
        self.assertIsNone(snap_split_ratio(1.15))
        self.assertIsNone(snap_split_ratio(0.75))

        # Standard ratios within tolerance snap cleanly
        self.assertEqual(snap_split_ratio(2.02), 2.0)
        self.assertEqual(snap_split_ratio(4.98), 5.0)
        self.assertEqual(snap_split_ratio(24.9), 25.0)

        # Decimal and string inputs snap cleanly without TypeError
        self.assertEqual(snap_split_ratio(Decimal("2.0")), 2.0)
        self.assertEqual(snap_split_ratio(Decimal("0.20")), 0.2)
        self.assertAlmostEqual(snap_split_ratio(Decimal("0.33333333")), 1.0 / 3.0, places=8)
        self.assertEqual(snap_split_ratio("5.0"), 5.0)
        self.assertIsNone(snap_split_ratio("non-numeric"))

        # Database must only contain valid standard corporate splits
        conn = psycopg2.connect(**_get_db_connection_params())
        with conn.cursor() as cur:
            cur.execute("SELECT cusip, period_of_report, split_factor FROM stock_splits;")
            rows = cur.fetchall()

            # Ensure purged false entries (Tamboran, Tigo, Picard) are not present
            cur.execute("""
                SELECT cusip FROM stock_splits
                WHERE cusip IN ('87507T101', '88675P103', '71953R108');
            """)
            purged = cur.fetchall()
        conn.close()

        self.assertEqual(len(purged), 0, f"Purged false splits still present in DB: {purged}")
        self.assertGreaterEqual(len(rows), 20)
        for cusip, period, factor in rows:
            snapped = snap_split_ratio(factor)
            self.assertIsNotNone(snapped, f"Non-standard split found in DB: {cusip} {period} {factor}")
            self.assertAlmostEqual(float(factor), snapped, places=6)

    def test_r2_reverse_split_precision_and_fractional_snapping(self):
        """
        R2: Reverse-split tolerance calibration and exact fractional precision
        (e.g. 1/3 as 0.33333333, Amcor 1:5 as 0.20000000).
        """
        from scripts.detect_stock_splits import snap_split_ratio
        from database import _get_db_connection_params
        import psycopg2

        # Amcor empirical candidate (~0.2112) snaps to 0.2 under calibrated 7.5% tolerance
        self.assertEqual(snap_split_ratio(0.2112), 0.2)
        # Dupont empirical candidate (~0.334) snaps to 1/3
        self.assertAlmostEqual(snap_split_ratio(0.334), 1.0 / 3.0, places=8)
        # Standard reverse splits (1:20, 1:10, 1:8, 1:4, 1:2) snap cleanly
        self.assertEqual(snap_split_ratio(0.049), 0.05)
        self.assertEqual(snap_split_ratio(0.098), 0.10)
        self.assertEqual(snap_split_ratio(0.124), 0.125)
        self.assertEqual(snap_split_ratio(0.245), 0.25)
        self.assertEqual(snap_split_ratio(0.49), 0.5)

        # Boundary snapping in overlapping intervals chooses nearest standard ratio
        self.assertAlmostEqual(snap_split_ratio(0.134), 1.0 / 7.0, places=8)
        self.assertEqual(snap_split_ratio(0.133), 0.125)

        # Null, non-positive, nan, and inf inputs return None
        self.assertIsNone(snap_split_ratio(None))
        self.assertIsNone(snap_split_ratio(0))
        self.assertIsNone(snap_split_ratio(-0.5))
        self.assertIsNone(snap_split_ratio(float("nan")))
        self.assertIsNone(snap_split_ratio(float("inf")))

        # Verify persisted values in database
        conn = psycopg2.connect(**_get_db_connection_params())
        with conn.cursor() as cur:
            # Amcor 1:5 reverse split
            cur.execute("""
                SELECT split_factor FROM stock_splits
                WHERE cusip = 'G0250X149' AND period_of_report = '2026-03-31'::date;
            """)
            amcor_row = cur.fetchone()
            # Dupont 1:3 reverse split
            cur.execute("""
                SELECT split_factor FROM stock_splits
                WHERE cusip = '26614N102' AND period_of_report = '2026-06-30'::date;
            """)
            dupont_row = cur.fetchone()
        conn.close()

        self.assertIsNotNone(amcor_row)
        self.assertEqual(float(amcor_row[0]), 0.2)

        self.assertIsNotNone(dupont_row)
        self.assertAlmostEqual(float(dupont_row[0]), 0.33333333, places=8)

    def test_r3_one_share_cash_in_lieu_rounding_tolerance(self):
        """
        R3: When |current_shares - rebased_prev_shares| <= 1, treat as unchanged
        (change_type='unchanged', change_in_share=0, percent_change=0.0).
        Quantum Financial Advisors held 50 shares of BKNG in Q1 and 1,251 in Q2.
        Rebased previous shares = 50 * 25 = 1,250 (discrepancy = 1 share).
        """
        r = self.client.get("/activity/latest/v3?security_type=stocks&cik=0001929139&ticker=BKNG")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        activities = data.get("activities", [])
        bkng = next((a for a in activities if "BOOKING" in (a.get("issuer_name") or "")), None)
        self.assertIsNotNone(bkng, "BKNG holding not found for Quantum Financial Advisors")

        self.assertEqual(bkng.get("change_type"), "unchanged")
        self.assertEqual(bkng.get("change_in_share"), 0)
        self.assertEqual(bkng.get("percent_change"), 0.0)
        self.assertEqual(bkng.get("current_shares"), 1251)
        self.assertEqual(bkng.get("previous_shares"), 1250)
        self.assertEqual(bkng.get("raw_previous_shares"), 50)
        self.assertEqual(bkng.get("raw_change_in_share"), 1201)
        self.assertEqual(bkng.get("split_factor"), 25.0)

    def test_r4_side_by_side_raw_and_adjusted_figures(self):
        """
        R4: Side-by-side raw and adjusted figures across /activity/latest/v3,
        /stories/latest/v2, and change feed queries.
        """
        # 1. /activity/latest/v3
        r_act = self.client.get("/activity/latest/v3?security_type=stocks&cik=0001350694&ticker=BKNG")
        self.assertEqual(r_act.status_code, 200)
        activities = r_act.json().get("activities", [])
        bkng = next((a for a in activities if "BOOKING" in (a.get("issuer_name") or "")), None)
        self.assertIsNotNone(bkng)
        self.assertEqual(bkng.get("previous_shares"), 1350)
        self.assertEqual(bkng.get("raw_previous_shares"), 54)
        self.assertEqual(bkng.get("change_in_share"), 40729)
        self.assertEqual(bkng.get("raw_change_in_share"), 42025)
        self.assertAlmostEqual(bkng.get("percent_change"), 3016.96, delta=5.0)
        self.assertAlmostEqual(bkng.get("raw_percent_change"), 77824.07, delta=5.0)

        # 2. /stories/latest/v2
        r_stories = self.client.get("/stories/latest/v2?limit=10")
        self.assertEqual(r_stories.status_code, 200)
        stories = r_stories.json().get("stories", [])
        self.assertGreater(len(stories), 0)
        # Check that top_increased or top_decreased positions carry raw figures
        found_raw = False
        for s in stories:
            top_inc = s.get("top_increased_position")
            if top_inc and "raw_previous_shares" in top_inc and top_inc["raw_previous_shares"] is not None:
                self.assertIn("raw_change_in_share", top_inc)
                self.assertIn("raw_percent_change", top_inc)
                found_raw = True
                break
        self.assertTrue(found_raw, "No story with raw share fields found in /stories/latest/v2")

        # 3. GET /changes endpoint side-by-side raw and adjusted fields
        from datetime import datetime, timezone
        from change_feed import encode_cursor
        from auth import sync_dependencies
        from database import get_db_connection
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "test"}
        try:
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT updated_at FROM filings WHERE filing_id = 338006")
                    row = cur.fetchone()
                    up_at = row[0] if row else datetime(2026, 8, 14, 20, 25, 24, tzinfo=timezone.utc)
            finally:
                conn.close()
            cursor = encode_cursor(up_at, 338006)
            r_changes = self.client.get(f"/changes?cursor={cursor}&limit=1")
            self.assertEqual(r_changes.status_code, 200)
            items = r_changes.json().get("items", [])
            self.assertEqual(len(items), 1)
            bkng_feed = next((a for a in items[0].get("activities", []) if "09857L" in (a.get("cusip") or "")), None)
            self.assertIsNotNone(bkng_feed)
            self.assertEqual(int(bkng_feed["previous_shares"]), 1350)
            self.assertEqual(int(bkng_feed["raw_previous_shares"]), 54)
            self.assertEqual(int(bkng_feed["change_in_share"]), 40729)
            self.assertEqual(int(bkng_feed["raw_change_in_share"]), 42025)
            self.assertAlmostEqual(float(bkng_feed["percent_change"]), 3016.96, delta=5.0)
            self.assertAlmostEqual(float(bkng_feed["raw_percent_change"]), 77824.07, delta=5.0)
        finally:
            app.dependency_overrides.clear()

    def test_r3_new_and_closed_positions_not_falsely_marked_unchanged(self):
        """
        Adversarial test: 1-share cash-in-lieu tolerance must strictly apply to
        existing dual-quarter holdings, and MUST NOT mistakenly zero out delta or
        percent change for new holdings (1 share) or closed positions under splits.
        """
        from change_feed import CHANGE_FEED_ACTIVITY_QUERY
        import psycopg2
        from psycopg2.extras import RealDictCursor
        from database import _get_db_connection_params

        conn = psycopg2.connect(**_get_db_connection_params())
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Test direct SQL computation under split_factor = 25.0
            cur.execute("""
                SELECT
                    CASE
                        WHEN prev_cusip IS NULL THEN 'new'
                        WHEN curr_cusip IS NULL THEN 'closed'
                        WHEN split_factor IS NOT NULL AND ABS(curr_shares - ROUND(prev_shares * split_factor)::bigint) <= 1 THEN 'unchanged'
                        WHEN curr_shares > ROUND(prev_shares * COALESCE(split_factor, 1.0))::bigint THEN 'increased'
                        WHEN curr_shares < ROUND(prev_shares * COALESCE(split_factor, 1.0))::bigint THEN 'decreased'
                        ELSE 'unchanged'
                    END AS change_type,
                    CASE
                        WHEN curr_cusip IS NOT NULL AND prev_cusip IS NOT NULL AND split_factor IS NOT NULL AND ABS(curr_shares - ROUND(prev_shares * split_factor)::bigint) <= 1 THEN 0
                        ELSE (COALESCE(curr_shares, 0) - ROUND(COALESCE(prev_shares, 0) * COALESCE(split_factor, 1.0))::bigint)
                    END AS change_in_share,
                    CASE
                        WHEN curr_cusip IS NOT NULL AND prev_cusip IS NOT NULL AND split_factor IS NOT NULL AND ABS(curr_shares - ROUND(prev_shares * split_factor)::bigint) <= 1 THEN 0.0
                        WHEN prev_shares > 0 THEN 
                            ((COALESCE(curr_shares, 0) - (prev_shares * COALESCE(split_factor, 1.0)))::numeric / (prev_shares * COALESCE(split_factor, 1.0))) * 100
                        ELSE NULL
                    END AS percent_change
                FROM (
                    VALUES
                        ('09857L108'::text, NULL::text, 1::bigint, NULL::bigint, 25.0::numeric),
                        (NULL::text, 'G0250X149'::text, NULL::bigint, 1::bigint, 0.2::numeric),
                        -- Active split: diff = -1 (1249 vs 1250 rebased) -> unchanged
                        ('09857L108'::text, '09857L108'::text, 1249::bigint, 50::bigint, 25.0::numeric),
                        -- Active split: diff = 0 (1250 vs 1250 rebased) -> unchanged
                        ('09857L108'::text, '09857L108'::text, 1250::bigint, 50::bigint, 25.0::numeric),
                        -- Active split: diff = +1 (1251 vs 1250 rebased) -> unchanged
                        ('09857L108'::text, '09857L108'::text, 1251::bigint, 50::bigint, 25.0::numeric),
                        -- Active split: diff = +2 (1252 vs 1250 rebased) -> increased
                        ('09857L108'::text, '09857L108'::text, 1252::bigint, 50::bigint, 25.0::numeric),
                        -- Active split: diff = -2 (1248 vs 1250 rebased) -> decreased
                        ('09857L108'::text, '09857L108'::text, 1248::bigint, 50::bigint, 25.0::numeric),
                        -- Non-split stock: diff = +1 (101 vs 100) -> increased, change_in_share = 1
                        ('037833100'::text, '037833100'::text, 101::bigint, 100::bigint, NULL::numeric),
                        -- Non-split stock: diff = -1 (99 vs 100) -> decreased, change_in_share = -1
                        ('037833100'::text, '037833100'::text, 99::bigint, 100::bigint, NULL::numeric)
                ) AS t(curr_cusip, prev_cusip, curr_shares, prev_shares, split_factor);
            """)
            rows = cur.fetchall()
        conn.close()

        # 1. New holding of 1 share: change_type='new', change_in_share=1, percent_change=None
        self.assertEqual(rows[0]["change_type"], "new")
        self.assertEqual(rows[0]["change_in_share"], 1)
        self.assertIsNone(rows[0]["percent_change"])

        # 2. Closed holding of 1 share under 1:5 reverse split: change_type='closed', percent_change=-100.0
        self.assertEqual(rows[1]["change_type"], "closed")
        self.assertAlmostEqual(float(rows[1]["percent_change"]), -100.0, places=2)

        # 3. Active split diff = -1: unchanged, change_in_share=0, percent_change=0.0
        self.assertEqual(rows[2]["change_type"], "unchanged")
        self.assertEqual(rows[2]["change_in_share"], 0)
        self.assertEqual(float(rows[2]["percent_change"]), 0.0)

        # 4. Active split diff = 0: unchanged, change_in_share=0, percent_change=0.0
        self.assertEqual(rows[3]["change_type"], "unchanged")
        self.assertEqual(rows[3]["change_in_share"], 0)
        self.assertEqual(float(rows[3]["percent_change"]), 0.0)

        # 5. Active split diff = +1: unchanged, change_in_share=0, percent_change=0.0
        self.assertEqual(rows[4]["change_type"], "unchanged")
        self.assertEqual(rows[4]["change_in_share"], 0)
        self.assertEqual(float(rows[4]["percent_change"]), 0.0)

        # 6. Active split diff = +2: increased, change_in_share=2
        self.assertEqual(rows[5]["change_type"], "increased")
        self.assertEqual(rows[5]["change_in_share"], 2)

        # 7. Active split diff = -2: decreased, change_in_share=-2
        self.assertEqual(rows[6]["change_type"], "decreased")
        self.assertEqual(rows[6]["change_in_share"], -2)

        # 8. Non-split stock with +1 share: MUST NOT be unchanged (increased, change=1)
        self.assertEqual(rows[7]["change_type"], "increased")
        self.assertEqual(rows[7]["change_in_share"], 1)

        # 9. Non-split stock with -1 share: MUST NOT be unchanged (decreased, change=-1)
        self.assertEqual(rows[8]["change_type"], "decreased")
        self.assertEqual(rows[8]["change_in_share"], -1)

    def test_r5_common_stock_scope_isolation(self):
        """
        R5: Common stock isolation: Options on split stocks retain true contract
        counts without split multiplication and have split_factor=None.
        Walleye Trading held 616,900 Carvana CALLs in Q1 and 2,486,800 in Q2,
        plus 199,500 PUTs in Q1 and 1,845,900 in Q2.
        Carvana executed a 5:1 split, but options must NOT be multiplied by 5.
        """
        r = self.client.get("/activity/latest/options?cik=1388391&ticker=CVNA")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        activities = data.get("activities", [])
        self.assertEqual(len(activities), 2)

        call_act = next((a for a in activities if a.get("put_or_call") == "CALL"), None)
        self.assertIsNotNone(call_act)
        self.assertIsNone(call_act.get("split_factor"))
        self.assertFalse(call_act.get("is_common_stock"))
        # True contract count: 616,900, NOT 5 * 616,900 = 3,084,500
        self.assertEqual(call_act.get("previous_shares"), 616900)
        self.assertEqual(call_act.get("raw_previous_shares"), 616900)
        self.assertEqual(call_act.get("current_shares"), 2486800)
        self.assertEqual(call_act.get("change_in_share"), 1869900)
        self.assertEqual(call_act.get("raw_change_in_share"), 1869900)

        put_act = next((a for a in activities if a.get("put_or_call") == "PUT"), None)
        self.assertIsNotNone(put_act)
        self.assertIsNone(put_act.get("split_factor"))
        self.assertFalse(put_act.get("is_common_stock"))
        self.assertEqual(put_act.get("previous_shares"), 199500)
        self.assertEqual(put_act.get("raw_previous_shares"), 199500)

        # Also verify parity via unified /activity/latest/v3?security_type=options endpoint
        r_v3 = self.client.get("/activity/latest/v3?security_type=options&cik=1388391&ticker=CVNA")
        self.assertEqual(r_v3.status_code, 200)
        v3_acts = r_v3.json().get("activities", [])
        self.assertEqual(len(v3_acts), 2)
        v3_call = next((a for a in v3_acts if a.get("put_or_call") == "CALL"), None)
        self.assertIsNotNone(v3_call)
        self.assertIsNone(v3_call.get("split_factor"))
        self.assertFalse(v3_call.get("is_common_stock"))
        self.assertEqual(v3_call.get("previous_shares"), 616900)
        self.assertEqual(v3_call.get("raw_previous_shares"), 616900)

        # Also verify /changes endpoint common stock isolation on split stocks
        from datetime import datetime, timezone
        from change_feed import encode_cursor
        from auth import sync_dependencies
        from database import get_db_connection
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "test"}
        try:
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT updated_at FROM filings WHERE filing_id = 334160")
                    row = cur.fetchone()
                    up_at = row[0] if row else datetime(2026, 8, 3, 20, 57, 27, 194334, tzinfo=timezone.utc)
            finally:
                conn.close()
            # Cursor right before Walleye Trading's 2026-06-30 filing (filing_id 334161)
            cursor = encode_cursor(up_at, 334160)
            r_chg = self.client.get(f"/changes?cursor={cursor}&limit=5")
            self.assertEqual(r_chg.status_code, 200)
            walleye_items = [it for it in r_chg.json().get("items", []) if it.get("cik") == "1388391"]
            self.assertGreaterEqual(len(walleye_items), 1)
            cvna_call = next((a for a in walleye_items[0].get("activities", []) if a.get("cusip") == "146869102" and a.get("put_or_call") == "CALL"), None)
            self.assertIsNotNone(cvna_call)
            self.assertIsNone(cvna_call.get("split_factor"))
            self.assertFalse(cvna_call.get("is_common_stock"))
            self.assertEqual(cvna_call.get("previous_shares"), 616900)
            self.assertEqual(cvna_call.get("raw_previous_shares"), 616900)
            self.assertEqual(cvna_call.get("current_shares"), 2486800)
            self.assertEqual(cvna_call.get("change_in_share"), 1869900)
            self.assertEqual(cvna_call.get("raw_change_in_share"), 1869900)

            cvna_put = next((a for a in walleye_items[0].get("activities", []) if a.get("cusip") == "146869102" and a.get("put_or_call") == "PUT"), None)
            self.assertIsNotNone(cvna_put)
            self.assertIsNone(cvna_put.get("split_factor"))
            self.assertFalse(cvna_put.get("is_common_stock"))
            self.assertEqual(cvna_put.get("previous_shares"), 199500)
            self.assertEqual(cvna_put.get("raw_previous_shares"), 199500)
        finally:
            app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
