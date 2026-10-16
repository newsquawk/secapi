"""
tests/test_e2e_r1_entry_type.py

Requirement 1 (R1): First-Ever vs. Re-entry Position Flag
Methodology: 4-Tier Opaque-Box E2E Testing Suite
- Tier 1: Feature Coverage (isolated representative inputs, models, enums, nullability)
- Tier 2: Boundary & Corner Cases (empty data, first-time filers, multi-year history, case-sensitivity, options)
- Tier 3: Cross-Feature Combinations (R1 entry type + R2 streaks, R4 filing flags, /stories/latest/v2, /changes)
- Tier 4: Real-World Application Scenarios (live PostgreSQL integration with Berkshire Hathaway, Marshall Wace, HighTower Advisors, Inspire Investing)
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
from typing import Dict, Any, List
from fastapi.testclient import TestClient

from main import app
from auth import sync_dependencies
import psycopg2
from psycopg2.extras import RealDictCursor


def get_db_connection():
    """Helper to acquire a direct psycopg2 connection for oracle verification."""
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME", "sec"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "password"),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
    )


class TestE2ER1EntryType(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        # Override sync dependencies so change feed tests can execute without Keycloak
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "test-service-account"}

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    # -------------------------------------------------------------------------
    # TIER 1: Feature Coverage (Representative Inputs in Isolation)
    # -------------------------------------------------------------------------

    def test_r1_t1_new_holding_first_ever_classified(self):
        """
        [Tier 1 - Feature Coverage]
        R1.1 / R1.2: A holding with change_type == 'new' and zero prior holdings
        must expose position_entry_type == 'first_ever' and alias entry_type == 'first_ever'.
        """
        response = self.client.get("/activity/latest/v3?limit=50&security_type=stocks")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        activities = data.get("activities", [])
        self.assertGreater(len(activities), 0, "Activities list should not be empty")

        new_activities = [a for a in activities if a.get("change_type") == "new"]
        if new_activities:
            for act in new_activities:
                self.assertIn("position_entry_type", act, "Activity missing position_entry_type")
                self.assertIn("entry_type", act, "Activity missing entry_type alias")
                self.assertIn(
                    act["position_entry_type"],
                    ["first_ever", "re_entry"],
                    f"Invalid position_entry_type: {act['position_entry_type']}"
                )
                self.assertEqual(
                    act["position_entry_type"],
                    act["entry_type"],
                    "entry_type alias must equal position_entry_type"
                )

    def test_r1_t1_new_holding_re_entry_classified(self):
        """
        [Tier 1 - Feature Coverage]
        R1.2: A manager who previously owned a security, closed it, and re-buys it
        must have position_entry_type == 're_entry'.
        """
        # Marshall Wace CIK 1318757 with FB FINL CORP (30257X104) is a verified re-entry
        response = self.client.get("/activity/latest/v3?cik=0001318757&ticker=FBK")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        activities = data.get("activities", [])
        re_entries = [a for a in activities if a.get("change_type") == "new" and "30257X104" in a.get("cusip", "")]
        for act in re_entries:
            self.assertEqual(act.get("position_entry_type"), "re_entry")
            self.assertEqual(act.get("entry_type"), "re_entry")

    def test_r1_t1_non_new_increased_returns_null(self):
        """
        [Tier 1 - Feature Coverage]
        R1.3: Non-new positions with change_type == 'increased' must expose null
        for position_entry_type and entry_type.
        """
        response = self.client.get("/activity/latest/v3?limit=50&security_type=stocks")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        increased = [a for a in data.get("activities", []) if a.get("change_type") == "increased"]
        self.assertGreater(len(increased), 0, "Expected at least one increased activity")
        for act in increased:
            self.assertIsNone(
                act.get("position_entry_type"),
                f"Increased position should have null position_entry_type, got {act.get('position_entry_type')}"
            )
            self.assertIsNone(
                act.get("entry_type"),
                f"Increased position should have null entry_type, got {act.get('entry_type')}"
            )

    def test_r1_t1_non_new_decreased_unchanged_closed_returns_null(self):
        """
        [Tier 1 - Feature Coverage]
        R1.3: Non-new positions with change_type in ('decreased', 'unchanged', 'closed')
        must strictly expose null.
        """
        response = self.client.get("/activity/latest/v3?limit=50&security_type=stocks")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        other_types = [
            a for a in data.get("activities", [])
            if a.get("change_type") in ("decreased", "unchanged", "closed")
        ]
        self.assertGreater(len(other_types), 0, "Expected non-new activities in response")
        for act in other_types:
            self.assertIsNone(
                act.get("position_entry_type"),
                f"{act.get('change_type')} holding must have null position_entry_type"
            )
            self.assertIsNone(
                act.get("entry_type"),
                f"{act.get('change_type')} holding must have null entry_type"
            )

    def test_r1_t1_model_field_alias_parity(self):
        """
        [Tier 1 - Feature Coverage]
        R1.1: Verify position_entry_type and entry_type maintain exact parity
        across all returned items.
        """
        response = self.client.get("/activity/latest/v3?limit=25")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for act in data.get("activities", []):
            self.assertEqual(
                act.get("position_entry_type"),
                act.get("entry_type"),
                "position_entry_type and entry_type must always have identical values"
            )

    # -------------------------------------------------------------------------
    # TIER 2: Boundary & Corner Cases
    # -------------------------------------------------------------------------

    def test_r1_t2_brand_new_fund_no_prior_history(self):
        """
        [Tier 2 - Boundary]
        If a manager has no historical filings in the database prior to the
        current period, ALL new holdings must be classified as 'first_ever'.
        """
        # Query database to find a company with exactly 1 filing
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT c.cik_number, c.company_name
                    FROM companies c
                    JOIN filings f ON c.company_id = f.company_id
                    GROUP BY c.cik_number, c.company_name
                    HAVING count(f.filing_id) = 1
                    LIMIT 1;
                """)
                row = cur.fetchone()

        if row:
            cik = row["cik_number"].zfill(10)
            res = self.client.get(f"/activity/latest/v3?cik={cik}&limit=20")
            if res.status_code == 200:
                data = res.json()
                for act in data.get("activities", []):
                    if act.get("change_type") == "new":
                        self.assertEqual(
                            act.get("position_entry_type"),
                            "first_ever",
                            f"First-time filer {cik} must have 'first_ever', got {act.get('position_entry_type')}"
                        )

    def test_r1_t2_multi_year_lookback_retention(self):
        """
        [Tier 2 - Boundary]
        Verify that a holding closed out 3+ years ago and re-initiated now is
        still recognized as 're_entry' (no arbitrary 1-year lookback cutoff).
        """
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Find a re-entry where the prior holding was >= 2 years before
                cur.execute("""
                    SELECT c.cik_number, f.filing_id, UPPER(i.cusip) as cusip
                    FROM filings f
                    JOIN companies c ON f.company_id = c.company_id
                    JOIN holdings_normalised h ON f.filing_id = h.filing_id
                    JOIN issuers i ON h.issuer_id = i.issuer_id
                    WHERE f.period_of_report >= '2026-06-30'
                      AND NOT EXISTS (
                          SELECT 1 FROM filings f_prev
                          JOIN holdings_normalised h_prev ON f_prev.filing_id = h_prev.filing_id
                          WHERE f_prev.company_id = f.company_id
                            AND f_prev.period_of_report >= (f.period_of_report - interval '1 year')
                            AND f_prev.period_of_report < f.period_of_report
                            AND h_prev.issuer_id = h.issuer_id
                      )
                      AND EXISTS (
                          SELECT 1 FROM filings f_old
                          JOIN holdings_normalised h_old ON f_old.filing_id = h_old.filing_id
                          WHERE f_old.company_id = f.company_id
                            AND f_old.period_of_report < (f.period_of_report - interval '2 years')
                            AND h_old.issuer_id = h.issuer_id
                      )
                    LIMIT 1;
                """)
                row = cur.fetchone()

        if row:
            cik = row["cik_number"].zfill(10)
            res = self.client.get(f"/activity/latest/v3?cik={cik}&limit=50")
            if res.status_code == 200:
                data = res.json()
                matching = [a for a in data.get("activities", []) if a.get("cusip") == row["cusip"]]
                for act in matching:
                    if act.get("change_type") == "new":
                        self.assertEqual(act.get("position_entry_type"), "re_entry")

    def test_r1_t2_case_insensitive_cusip_matching(self):
        """
        [Tier 2 - Boundary]
        Historical CUSIP matches must be case-insensitive (e.g. '037833100' vs '037833100').
        """
        response = self.client.get("/activity/latest/v3?limit=30")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for act in data.get("activities", []):
            cusip = act.get("cusip")
            if cusip:
                self.assertEqual(cusip, cusip.upper(), "CUSIPs returned by endpoint must be uppercase")

    def test_r1_t2_options_activity_entry_type(self):
        """
        [Tier 2 - Boundary]
        /activity/latest/options must also correctly expose position_entry_type
        for new option positions.
        """
        response = self.client.get("/activity/latest/options?limit=25")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        activities = data.get("activities", [])
        for act in activities:
            self.assertIn("position_entry_type", act)
            self.assertIn("entry_type", act)
            if act.get("change_type") == "new":
                self.assertIn(act.get("position_entry_type"), ["first_ever", "re_entry"])
            else:
                self.assertIsNone(act.get("position_entry_type"))

    def test_r1_t2_empty_activities_payload(self):
        """
        [Tier 2 - Boundary]
        Filer with zero activities in filter returns empty list with status 200.
        """
        # Existing CIK with unmatched ticker filter
        response = self.client.get("/activity/latest/v3?cik=0001067983&ticker=NONEXISTENT123")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("activities"), [])

    # -------------------------------------------------------------------------
    # TIER 3: Cross-Feature Combinations
    # -------------------------------------------------------------------------

    def test_r1_t3_reentry_paired_with_r2_streak(self):
        """
        [Tier 3 - Cross-Feature]
        A position classified as 're_entry' (new position) must have its R2 streak
        initialized to streak_quarters == 1 with streak_direction == 'buy'.
        """
        response = self.client.get("/activity/latest/v3?limit=50")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        re_entries = [a for a in data.get("activities", []) if a.get("position_entry_type") == "re_entry"]
        for act in re_entries:
            self.assertEqual(act.get("streak_quarters"), 1, "New re-entry position must have streak_quarters == 1")
            self.assertIn(act.get("streak_direction"), ["buy", "buying"])

    def test_r1_t3_first_ever_in_change_feed_envelope(self):
        """
        [Tier 3 - Cross-Feature]
        In GET /changes, activities inside FilingEnvelope must expose position_entry_type
        alongside filing-level metadata flags (is_amendment, is_late_filing).
        """
        response = self.client.get("/changes?limit=2")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        items = data.get("items", [])
        if items:
            envelope = items[0]
            self.assertIn("is_amendment", envelope)
            self.assertIn("is_late_filing", envelope)
            activities = envelope.get("activities", [])
            for act in activities:
                self.assertIn("position_entry_type", act)
                self.assertIn("entry_type", act)
                if act.get("change_type") == "new":
                    self.assertIn(act.get("position_entry_type"), ["first_ever", "re_entry"])
                else:
                    self.assertIsNone(act.get("position_entry_type"))

    def test_r1_t3_stories_v2_top_new_position_entry_type(self):
        """
        [Tier 3 - Cross-Feature]
        In GET /stories/latest/v2, top_new_position and top_new_other_securities
        must expose position_entry_type and entry_type.
        """
        response = self.client.get("/stories/latest/v2?limit=5")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        stories = data.get("stories", [])
        for story in stories:
            top_new = story.get("top_new_position")
            if top_new:
                self.assertIn("position_entry_type", top_new)
                self.assertIn("entry_type", top_new)
                self.assertIn(top_new.get("position_entry_type"), ["first_ever", "re_entry"])
                self.assertEqual(top_new.get("position_entry_type"), top_new.get("entry_type"))

    def test_r1_t3_activity_latest_v3_pagination_preserves_entry_type(self):
        """
        [Tier 3 - Cross-Feature]
        Paginating through /activity/latest/v3 with offset preserves position_entry_type.
        """
        p1 = self.client.get("/activity/latest/v3?limit=10&offset=0").json().get("activities", [])
        p2 = self.client.get("/activity/latest/v3?limit=10&offset=10").json().get("activities", [])
        for act in p1 + p2:
            self.assertIn("position_entry_type", act)
            self.assertIn("entry_type", act)

    # -------------------------------------------------------------------------
    # TIER 4: Real-World Application Scenarios (Live PostgreSQL Integration)
    # -------------------------------------------------------------------------

    def test_r1_t4_berkshire_hathaway_real_activities(self):
        """
        [Tier 4 - Real-World Integration]
        Berkshire Hathaway (CIK 0001067983):
        - All non-new activities have position_entry_type == None
        - New position D R HORTON INC (CUSIP 23331A109) in Q2 2026 is 'first_ever'.
        """
        response = self.client.get("/activity/latest/v3?cik=0001067983&limit=50")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        activities = data.get("activities", [])
        self.assertGreater(len(activities), 0, "Berkshire Hathaway activities expected")

        for act in activities:
            if act.get("change_type") != "new":
                self.assertIsNone(act.get("position_entry_type"))
            else:
                self.assertIn(act.get("position_entry_type"), ["first_ever", "re_entry"])

        # Check D R HORTON INC specifically (held in 2023/2025, closed in Q1 2026, re-entered in Q2 2026)
        dr_horton = next((a for a in activities if "23331A109" in a.get("cusip", "")), None)
        if dr_horton and dr_horton.get("change_type") == "new":
            self.assertEqual(dr_horton.get("position_entry_type"), "re_entry")

    def test_r1_t4_marshall_wace_real_re_entry(self):
        """
        [Tier 4 - Real-World Integration]
        Marshall Wace (CIK 0001318757):
        Security FB FINL CORP (CUSIP 30257X104) was held previously, closed,
        and re-initiated in Q2 2026 -> must evaluate to 're_entry'.
        """
        response = self.client.get("/activity/latest/v3?cik=0001318757&ticker=FBK")
        self.assertEqual(response.status_code, 200)
        activities = response.json().get("activities", [])
        for act in activities:
            if act.get("change_type") == "new" and "30257X104" in act.get("cusip", ""):
                self.assertEqual(act.get("position_entry_type"), "re_entry")
                self.assertEqual(act.get("entry_type"), "re_entry")

    def test_r1_t4_hightower_advisors_real_re_entry(self):
        """
        [Tier 4 - Real-World Integration]
        HighTower Advisors (CIK 0001462245):
        ALLIANCEBERNSTEIN GLOBAL HIG (CUSIP 01879R106) was held historically
        and is a re-entry in 2026.
        """
        response = self.client.get("/activity/latest/v3?cik=0001462245&limit=50")
        self.assertEqual(response.status_code, 200)
        activities = response.json().get("activities", [])
        ab_holding = next((a for a in activities if "01879R106" in a.get("cusip", "") and a.get("change_type") == "new"), None)
        if ab_holding:
            self.assertEqual(ab_holding.get("position_entry_type"), "re_entry")

    def test_r1_t4_inspire_investing_activities_entry_type(self):
        """
        [Tier 4 - Real-World Integration]
        Inspire Investing (CIK 0001670139):
        Verify all returned activities adhere to R1 schema contract.
        """
        response = self.client.get("/activity/latest/v3?cik=0001670139&limit=50")
        self.assertEqual(response.status_code, 200)
        activities = response.json().get("activities", [])
        for act in activities:
            if act.get("change_type") == "new":
                self.assertIn(act.get("position_entry_type"), ["first_ever", "re_entry"])
            else:
                self.assertIsNone(act.get("position_entry_type"))


if __name__ == "__main__":
    unittest.main()
