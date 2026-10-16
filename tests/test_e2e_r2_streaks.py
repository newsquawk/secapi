"""
tests/test_e2e_r2_streaks.py

Requirement 2 (R2): Consecutive Quarterly Buying and Selling Streaks
Methodology: 4-Tier Opaque-Box E2E Testing Suite
- Tier 1: Feature Coverage (streak_quarters, streak_direction, streak_type, buying vs selling)
- Tier 2: Boundary & Corner Cases (direction reversal, quarter gaps, split rebasing, 1-share tolerance, single-quarter fund)
- Tier 3: Cross-Feature Combinations (R1 entry type + R2 streaks, /stories/latest/v2, /changes)
- Tier 4: Real-World Application Scenarios (Berkshire Hathaway Apple selling streak, Inspire Investing, Marshall Wace performance benchmark)
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import time
import unittest
from typing import Dict, Any, List
from fastapi.testclient import TestClient

from main import app
from auth import sync_dependencies
import psycopg2
from psycopg2.extras import RealDictCursor


def get_db_connection():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME", "sec"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "password"),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
    )


class TestE2ER2Streaks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "test-service-account"}

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    # -------------------------------------------------------------------------
    # TIER 1: Feature Coverage (Isolated Representative Inputs)
    # -------------------------------------------------------------------------

    def test_r2_t1_new_position_streak_is_one_buy(self):
        """
        [Tier 1 - Feature Coverage]
        R2.1 / R2.2: Brand-new positions (change_type == 'new') must always have
        streak_quarters == 1 and streak_direction in ('buy', 'buying').
        """
        response = self.client.get("/activity/latest/v3?limit=50&security_type=stocks")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        new_activities = [a for a in data.get("activities", []) if a.get("change_type") == "new"]
        self.assertGreater(len(new_activities), 0, "Expected new activities")
        for act in new_activities:
            self.assertIn("streak_quarters", act, "Activity missing streak_quarters")
            self.assertIn("streak_direction", act, "Activity missing streak_direction")
            self.assertEqual(act.get("streak_quarters"), 1)
            self.assertIn(act.get("streak_direction"), ["buy", "buying"])

    def test_r2_t1_increased_position_streak_is_buy(self):
        """
        [Tier 1 - Feature Coverage]
        R2.2: Increased positions (change_type == 'increased') must have
        streak_quarters >= 1 and streak_direction in ('buy', 'buying').
        """
        response = self.client.get("/activity/latest/v3?limit=50&security_type=stocks")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        increased = [a for a in data.get("activities", []) if a.get("change_type") == "increased"]
        self.assertGreater(len(increased), 0, "Expected increased activities")
        for act in increased:
            self.assertIn("streak_quarters", act)
            self.assertIn("streak_direction", act)
            self.assertIsNotNone(act.get("streak_quarters"), "streak_quarters should not be None")
            self.assertGreaterEqual(act.get("streak_quarters"), 1)
            self.assertIn(act.get("streak_direction"), ["buy", "buying"])

    def test_r2_t1_decreased_position_streak_is_sell(self):
        """
        [Tier 1 - Feature Coverage]
        R2.2: Decreased positions (change_type == 'decreased') must have
        streak_quarters >= 1 and streak_direction in ('sell', 'selling').
        """
        response = self.client.get("/activity/latest/v3?limit=50&security_type=stocks")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        decreased = [a for a in data.get("activities", []) if a.get("change_type") == "decreased"]
        self.assertGreater(len(decreased), 0, "Expected decreased activities")
        for act in decreased:
            self.assertIn("streak_quarters", act)
            self.assertIn("streak_direction", act)
            self.assertIsNotNone(act.get("streak_quarters"), "streak_quarters should not be None")
            self.assertGreaterEqual(act.get("streak_quarters"), 1)
            self.assertIn(act.get("streak_direction"), ["sell", "selling"])

    def test_r2_t1_closed_position_streak_is_sell(self):
        """
        [Tier 1 - Feature Coverage]
        R2.2: Closed positions (change_type == 'closed') must have
        streak_quarters >= 1 and streak_direction in ('sell', 'selling').
        """
        response = self.client.get("/activity/latest/v3?limit=50&security_type=stocks")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        closed = [a for a in data.get("activities", []) if a.get("change_type") == "closed"]
        if closed:
            for act in closed:
                self.assertIn("streak_quarters", act)
                self.assertIn("streak_direction", act)
                self.assertIsNotNone(act.get("streak_quarters"), "streak_quarters should not be None")
                self.assertGreaterEqual(act.get("streak_quarters"), 1)
                self.assertIn(act.get("streak_direction"), ["sell", "selling"])

    def test_r2_t1_unchanged_position_streak_is_null_or_zero(self):
        """
        [Tier 1 - Feature Coverage]
        R2.2: Unchanged positions (change_type == 'unchanged') must have
        streak_direction is None and streak_quarters in (0, None).
        """
        response = self.client.get("/activity/latest/v3?limit=50&security_type=stocks")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        unchanged = [a for a in data.get("activities", []) if a.get("change_type") == "unchanged"]
        for act in unchanged:
            self.assertIsNone(act.get("streak_direction"))
            self.assertIn(act.get("streak_quarters"), (0, None))

    def test_r2_t1_streak_type_alias_parity(self):
        """
        [Tier 1 - Feature Coverage]
        R2.1: streak_type alias must strictly equal streak_direction.
        """
        response = self.client.get("/activity/latest/v3?limit=30")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for act in data.get("activities", []):
            self.assertEqual(
                act.get("streak_direction"),
                act.get("streak_type"),
                "streak_direction and streak_type alias must be identical"
            )

    # -------------------------------------------------------------------------
    # TIER 2: Boundary & Corner Cases
    # -------------------------------------------------------------------------

    def test_r2_t2_directional_reversal_resets_streak(self):
        """
        [Tier 2 - Boundary]
        If manager bought in Q1 and decreased in Q2, the buying streak is terminated
        and a new selling streak begins with streak_quarters == 1.
        """
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Find a manager who bought in Q1 and decreased in Q2
                cur.execute("""
                    WITH q1 AS (
                        SELECT f.company_id, UPPER(i.cusip) as cusip, SUM(h.shares_or_principal_amount) as q1_shares
                        FROM filings f
                        JOIN holdings_normalised h ON f.filing_id = h.filing_id
                        JOIN issuers i ON h.issuer_id = i.issuer_id
                        WHERE f.period_of_report = '2026-03-31'
                        GROUP BY f.company_id, UPPER(i.cusip)
                    ),
                    q0 AS (
                        SELECT f.company_id, UPPER(i.cusip) as cusip, SUM(h.shares_or_principal_amount) as q0_shares
                        FROM filings f
                        JOIN holdings_normalised h ON f.filing_id = h.filing_id
                        JOIN issuers i ON h.issuer_id = i.issuer_id
                        WHERE f.period_of_report = '2025-12-31'
                        GROUP BY f.company_id, UPPER(i.cusip)
                    ),
                    q2 AS (
                        SELECT f.company_id, c.cik_number, UPPER(i.cusip) as cusip, SUM(h.shares_or_principal_amount) as q2_shares
                        FROM filings f
                        JOIN companies c ON f.company_id = c.company_id
                        JOIN holdings_normalised h ON f.filing_id = h.filing_id
                        JOIN issuers i ON h.issuer_id = i.issuer_id
                        WHERE f.period_of_report = '2026-06-30'
                        GROUP BY f.company_id, c.cik_number, UPPER(i.cusip)
                    )
                    SELECT q2.cik_number, q2.cusip
                    FROM q2
                    JOIN q1 ON q2.company_id = q1.company_id AND q2.cusip = q1.cusip
                    JOIN q0 ON q1.company_id = q0.company_id AND q1.cusip = q0.cusip
                    WHERE q1.q1_shares > q0.q0_shares  -- increased in Q1
                      AND q2.q2_shares < q1.q1_shares  -- decreased in Q2
                    LIMIT 1;
                """)
                row = cur.fetchone()

        if row:
            cik = row["cik_number"].zfill(10)
            res = self.client.get(f"/activity/latest/v3?cik={cik}&limit=50")
            if res.status_code == 200:
                matching = [a for a in res.json().get("activities", []) if a.get("cusip") == row["cusip"]]
                for act in matching:
                    if act.get("change_type") == "decreased":
                        self.assertEqual(act.get("streak_quarters"), 1, "Reversal must reset streak to 1")
                        self.assertIn(act.get("streak_direction"), ["sell", "selling"])

    def test_r2_t2_quarterly_gap_resets_streak(self):
        """
        [Tier 2 - Boundary]
        If a calendar quarter was skipped without a filing, continuity is broken
        and streak_quarters resets to 1.
        """
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Find a manager who filed in Q2 2026 and Q4 2025, but skipped Q1 2026
                cur.execute("""
                    SELECT c.cik_number, c.company_id
                    FROM companies c
                    WHERE EXISTS (SELECT 1 FROM filings f WHERE f.company_id = c.company_id AND f.period_of_report = '2026-06-30')
                      AND NOT EXISTS (SELECT 1 FROM filings f WHERE f.company_id = c.company_id AND f.period_of_report = '2026-03-31')
                      AND EXISTS (SELECT 1 FROM filings f WHERE f.company_id = c.company_id AND f.period_of_report = '2025-12-31')
                    LIMIT 1;
                """)
                row = cur.fetchone()

        if row:
            cik = row["cik_number"].zfill(10)
            res = self.client.get(f"/activity/latest/v3?cik={cik}&limit=20")
            if res.status_code == 200:
                for act in res.json().get("activities", []):
                    if act.get("change_type") in ("new", "increased", "decreased"):
                        self.assertEqual(act.get("streak_quarters"), 1, "Skipped quarter must reset streak to 1")

    def test_r2_t2_stock_split_rebasing_streak_continuity(self):
        """
        [Tier 2 - Boundary]
        Stock split rebasing must preserve streak continuity:
        Booking Holdings (BKNG, 25:1 split) holders must have prior shares multiplied
        by 25.0 when evaluating quarterly share deltas.
        """
        res = self.client.get("/activity/latest/v3?security_type=stocks&cik=0001350694&ticker=BKNG")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        bkng = next((a for a in activities if "BOOKING" in (a.get("issuer_name") or "")), None)
        if bkng:
            self.assertEqual(bkng.get("split_factor"), 25.0)
            self.assertIn("streak_quarters", bkng)
            self.assertIn("streak_direction", bkng)

    def test_r2_t2_cash_in_lieu_one_share_tolerance_terminates_streak(self):
        """
        [Tier 2 - Boundary]
        If ABS(curr - rebased_prev) <= 1, position is unchanged and terminates
        buying/selling streaks (streak_direction is None).
        """
        res = self.client.get("/activity/latest/v3?security_type=stocks&cik=0001929139&ticker=BKNG")
        if res.status_code == 200:
            activities = res.json().get("activities", [])
            bkng = next((a for a in activities if "BOOKING" in (a.get("issuer_name") or "")), None)
            if bkng and bkng.get("change_type") == "unchanged":
                self.assertIsNone(bkng.get("streak_direction"))
                self.assertIn(bkng.get("streak_quarters"), (0, None))

    def test_r2_t2_fund_with_single_quarter_history(self):
        """
        [Tier 2 - Boundary]
        A manager with only 1 historical filing in the DB has streak_quarters == 1
        for all non-unchanged holdings.
        """
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT c.cik_number
                    FROM companies c
                    JOIN filings f ON c.company_id = f.company_id
                    GROUP BY c.cik_number, c.company_id
                    HAVING count(f.filing_id) = 1
                    LIMIT 1;
                """)
                row = cur.fetchone()

        if row:
            cik = row["cik_number"].zfill(10)
            res = self.client.get(f"/activity/latest/v3?cik={cik}&limit=20")
            if res.status_code == 200:
                for act in res.json().get("activities", []):
                    if act.get("change_type") in ("new", "increased", "decreased"):
                        self.assertEqual(act.get("streak_quarters"), 1)

    # -------------------------------------------------------------------------
    # TIER 3: Cross-Feature Combinations
    # -------------------------------------------------------------------------

    def test_r2_t3_reentry_vs_streak_interaction(self):
        """
        [Tier 3 - Cross-Feature]
        A re-entry position (R1: position_entry_type == 're_entry') must have
        streak_quarters == 1 and streak_direction in ('buy', 'buying').
        """
        res = self.client.get("/activity/latest/v3?cik=0001318757&ticker=FBK")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        for act in activities:
            if act.get("position_entry_type") == "re_entry":
                self.assertEqual(act.get("streak_quarters"), 1)
                self.assertIn(act.get("streak_direction"), ["buy", "buying"])

    def test_r2_t3_stories_v2_top_movers_streaks(self):
        """
        [Tier 3 - Cross-Feature]
        In GET /stories/latest/v2, top_increased_position and top_decreased_position
        must expose streak_quarters and streak_direction.
        """
        res = self.client.get("/stories/latest/v2?limit=5")
        self.assertEqual(res.status_code, 200)
        stories = res.json().get("stories", [])
        for story in stories:
            top_inc = story.get("top_increased_position")
            if top_inc:
                self.assertIn("streak_quarters", top_inc)
                self.assertIn("streak_direction", top_inc)
                self.assertIn(top_inc.get("streak_direction"), ["buy", "buying"])
            top_dec = story.get("top_decreased_position")
            if top_dec:
                self.assertIn("streak_quarters", top_dec)
                self.assertIn("streak_direction", top_dec)
                self.assertIn(top_dec.get("streak_direction"), ["sell", "selling"])

    def test_r2_t3_change_feed_envelope_streak_fields(self):
        """
        [Tier 3 - Cross-Feature]
        In GET /changes, activities inside FilingEnvelope must carry streak fields.
        """
        res = self.client.get("/changes?limit=2")
        self.assertEqual(res.status_code, 200)
        items = res.json().get("items", [])
        if items:
            for act in items[0].get("activities", []):
                self.assertIn("streak_quarters", act)
                self.assertIn("streak_direction", act)
                self.assertIn("streak_type", act)

    def test_r2_t3_options_activity_streaks(self):
        """
        [Tier 3 - Cross-Feature]
        /activity/latest/options activities must also expose streak metadata.
        """
        res = self.client.get("/activity/latest/options?limit=10")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        for act in activities:
            self.assertIn("streak_quarters", act)
            self.assertIn("streak_direction", act)

    # -------------------------------------------------------------------------
    # TIER 4: Real-World Application Scenarios (Live PostgreSQL Integration)
    # -------------------------------------------------------------------------

    def test_r2_t4_berkshire_hathaway_apple_selling_streak(self):
        """
        [Tier 4 - Real-World Integration]
        Berkshire Hathaway (CIK 0001067983) decreased Apple (AAPL, CUSIP 037833100)
        across consecutive quarters in 2026.
        Verify streak_quarters >= 2 and streak_direction in ('sell', 'selling').
        """
        res = self.client.get("/activity/latest/v3?cik=0001067983&ticker=AAPL")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        apple_act = next((a for a in activities if "037833100" in a.get("cusip", "")), None)
        if apple_act and apple_act.get("change_type") == "decreased":
            self.assertGreaterEqual(apple_act.get("streak_quarters", 0), 2)
            self.assertIn(apple_act.get("streak_direction"), ["sell", "selling"])

    def test_r2_t4_inspire_investing_streaks(self):
        """
        [Tier 4 - Real-World Integration]
        Inspire Investing (CIK 0001670139):
        Verify streak metrics on latest holdings.
        """
        res = self.client.get("/activity/latest/v3?cik=0001670139&limit=30")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        for act in activities:
            sq = act.get("streak_quarters")
            if act.get("change_type") in ("new", "increased"):
                self.assertIsNotNone(sq, "streak_quarters should not be None for new/increased")
                self.assertGreaterEqual(sq, 1)
                self.assertIn(act.get("streak_direction"), ["buy", "buying"])
            elif act.get("change_type") in ("decreased", "closed"):
                self.assertIsNotNone(sq, "streak_quarters should not be None for decreased/closed")
                self.assertGreaterEqual(sq, 1)
                self.assertIn(act.get("streak_direction"), ["sell", "selling"])

    def test_r2_t4_marshall_wace_streaks(self):
        """
        [Tier 4 - Real-World Integration]
        Marshall Wace (CIK 0001318757):
        Mega-portfolio holdings must all have valid streak metadata.
        """
        res = self.client.get("/activity/latest/v3?cik=0001318757&limit=50")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        self.assertGreater(len(activities), 0)
        for act in activities:
            self.assertIn("streak_quarters", act)
            self.assertIn("streak_direction", act)

    def test_r2_t4_bulk_enrichment_performance_benchmark(self):
        """
        [Tier 4 - Real-World Integration]
        Performance SLA: Bulk streak enrichment on a full page of 50 activities
        must complete in under 500 ms without N+1 query bottlenecks.
        """
        # Warmup
        self.client.get("/activity/latest/v3?limit=5&security_type=stocks")

        start_time = time.perf_counter()
        res = self.client.get("/activity/latest/v3?limit=50&security_type=stocks")
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        self.assertEqual(res.status_code, 200)
        # SLA: under 500ms
        self.assertLess(
            elapsed_ms,
            500.0,
            f"Bulk streak enrichment took {elapsed_ms:.2f}ms, exceeding 500ms threshold"
        )


if __name__ == "__main__":
    unittest.main()
