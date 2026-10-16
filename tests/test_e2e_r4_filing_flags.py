"""
tests/test_e2e_r4_filing_flags.py

Requirement 4 (R4): Amended and Late Filing Flags
Methodology: 4-Tier Opaque-Box E2E Testing Suite
- Tier 1: Feature Coverage (is_amendment, amendment_type, is_late_filing across filing models)
- Tier 2: Boundary & Corner Cases (Presidents' Day roll-forward, Sat/Sun roll-forward, standalone amendments, exact deadline day)
- Tier 3: Cross-Feature Combinations (change feed envelope vs activity flags, /activity/latest/v3, /managers/{cik}/filings)
- Tier 4: Real-World Application Scenarios (Berkshire Hathaway Presidents' Day on-time filing, Inspire Investing amendment, HighTower addition amendment)
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import datetime as dt
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


def compute_statutory_deadline(period_of_report: dt.date) -> dt.date:
    """
    Authoritative SEC Rule 13f-1(a)(1) & Rule 0-3(a) deadline oracle:
    1. Base deadline = period_of_report + 45 days.
    2. Weekend roll-forward: Saturday -> Monday (+2), Sunday -> Monday (+1).
    3. Washington's Birthday (Presidents' Day): Third Monday in February (days 15-21) -> Tuesday (+1).
    """
    deadline = period_of_report + dt.timedelta(days=45)
    # Weekend roll-forward
    if deadline.weekday() == 5:  # Saturday
        deadline += dt.timedelta(days=2)
    elif deadline.weekday() == 6:  # Sunday
        deadline += dt.timedelta(days=1)

    # Federal holiday roll-forward (Presidents' Day)
    if deadline.month == 2 and deadline.weekday() == 0 and 15 <= deadline.day <= 21:
        deadline += dt.timedelta(days=1)

    return deadline


class TestE2ER4FilingFlags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "test-service-account"}

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    # -------------------------------------------------------------------------
    # TIER 1: Feature Coverage (Isolated Representative Inputs & Models)
    # -------------------------------------------------------------------------

    def test_r4_t1_base_filing_flags(self):
        """
        [Tier 1 - Feature Coverage]
        R4.1 / R4.2 / R4.3: An original 13F-HR filing submitted on time must have:
        - is_amendment == False
        - amendment_type is None
        - is_late_filing == False
        """
        # Berkshire Hathaway Q2 2026 filing (0001193125-26-352200): 13F-HR on 2026-08-14
        res = self.client.get("/filings/0001193125-26-352200")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("is_amendment", data)
        self.assertIn("amendment_type", data)
        self.assertIn("is_late_filing", data)

        self.assertIs(data["is_amendment"], False)
        self.assertIsNone(data["amendment_type"])
        self.assertIs(data["is_late_filing"], False)

    def test_r4_t1_amendment_flag_true(self):
        """
        [Tier 1 - Feature Coverage]
        R4.2: Any filing with form_type containing '/A' (e.g. 13F-HR/A)
        must have is_amendment == True.
        """
        # Inspire Investing amendment 0001670139-26-000006
        res = self.client.get("/filings/0001670139-26-000006")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("is_amendment", data)
        self.assertTrue(data["is_amendment"])

    def test_r4_t1_amendment_type_restatement(self):
        """
        [Tier 1 - Feature Coverage]
        R4.2: A full restatement amendment must expose amendment_type == 'restatement'.
        """
        res = self.client.get("/filings/0001670139-26-000006")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("is_amendment"))
        self.assertEqual(data.get("amendment_type"), "restatement")

    def test_r4_t1_amendment_type_addition(self):
        """
        [Tier 1 - Feature Coverage]
        R4.2: A partial new-holdings addition amendment must expose amendment_type == 'addition'.
        """
        # HighTower Advisors 0001085146-26-000657 (1 holding vs base 3,797)
        res = self.client.get("/filings/0001085146-26-000657")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("is_amendment"))
        self.assertEqual(data.get("amendment_type"), "addition")

    def test_r4_t1_late_filing_flag_true(self):
        """
        [Tier 1 - Feature Coverage]
        R4.3: Filing submitted past statutory deadline must have is_late_filing == True.
        """
        # HighTower Advisors 0001085146-26-000657 filed Aug 26, 2026 for period 2026-06-30
        res = self.client.get("/filings/0001085146-26-000657")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("is_late_filing"))

    # -------------------------------------------------------------------------
    # TIER 2: Boundary & Corner Cases (Roll-Forward & Edge Conditions)
    # -------------------------------------------------------------------------

    def test_r4_t2_presidents_day_holiday_roll_forward(self):
        """
        [Tier 2 - Boundary]
        Period 2025-12-31:
        - 45 days is Saturday 2026-02-14.
        - Sunday is 2026-02-15.
        - Monday 2026-02-16 is Presidents' Day (Washington's Birthday).
        - Statutory deadline rolls forward to Tuesday, February 17, 2026.
        A filing submitted on 2026-02-17 is on-time (is_late_filing == False).
        A filing submitted on 2026-02-18 is late (is_late_filing == True).
        """
        deadline = compute_statutory_deadline(dt.date(2025, 12, 31))
        self.assertEqual(deadline, dt.date(2026, 2, 17))

        # Check Berkshire Hathaway 0001193125-26-054580 filed on 2026-02-17
        res = self.client.get("/filings/0001193125-26-054580")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("filing_date"), "2026-02-17")
        self.assertIs(data.get("is_late_filing"), False, "Presidents' Day deadline was Feb 17; filing on Feb 17 is on time!")

    def test_r4_t2_saturday_deadline_roll_forward(self):
        """
        [Tier 2 - Boundary]
        When 45th calendar day lands on Saturday, deadline rolls to Monday (+2 days).
        Example: Period 2023-09-30 -> Day 45 is Tuesday Nov 14.
        Period 2024-03-31 -> Day 45 is Wednesday May 15.
        Period 2025-03-31 -> Day 45 is Thursday May 15.
        Period 2026-09-30 -> Day 45 is Saturday Nov 14 -> Rolls to Monday Nov 16.
        """
        deadline_sat = compute_statutory_deadline(dt.date(2026, 9, 30))
        self.assertEqual(deadline_sat.weekday(), 0)  # Monday
        self.assertEqual(deadline_sat, dt.date(2026, 11, 16))

    def test_r4_t2_sunday_deadline_roll_forward(self):
        """
        [Tier 2 - Boundary]
        When 45th calendar day lands on Sunday, deadline rolls to Monday (+1 day).
        Example: Period 2027-09-30 -> Day 45 is Sunday Nov 14 -> Rolls to Monday Nov 15.
        """
        deadline_sun = compute_statutory_deadline(dt.date(2027, 9, 30))
        self.assertEqual(deadline_sun.weekday(), 0)  # Monday
        self.assertEqual(deadline_sun, dt.date(2027, 11, 15))

    def test_r4_t2_amendment_without_base_filing(self):
        """
        [Tier 2 - Boundary]
        When a manager submits a 13F-HR/A without an existing base 13F-HR in the DB,
        amendment_type must default to 'restatement'.
        """
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT f.accession_number
                    FROM filings f
                    WHERE f.form_type IN ('13F-HR/A', '13F-HR/A/A')
                      AND NOT EXISTS (
                          SELECT 1 FROM filings f_base
                          WHERE f_base.company_id = f.company_id
                            AND f_base.period_of_report = f.period_of_report
                            AND f_base.form_type = '13F-HR'
                      )
                    LIMIT 1;
                """)
                row = cur.fetchone()

        if row:
            res = self.client.get(f"/filings/{row['accession_number']}")
            if res.status_code == 200:
                self.assertEqual(res.json().get("amendment_type"), "restatement")

    def test_r4_t2_filing_date_exact_deadline_is_not_late(self):
        """
        [Tier 2 - Boundary]
        Filing submitted on the exact day of the statutory deadline has is_late_filing == False.
        """
        # Q2 2026 ended 2026-06-30. 45 days is Friday 2026-08-14.
        res = self.client.get("/filings/0001193125-26-352200")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("filing_date"), "2026-08-14")
        self.assertIs(data.get("is_late_filing"), False)

    # -------------------------------------------------------------------------
    # TIER 3: Cross-Feature Combinations
    # -------------------------------------------------------------------------

    def test_r4_t3_change_feed_envelope_and_activity_flags(self):
        """
        [Tier 3 - Cross-Feature]
        In GET /changes, FilingEnvelope exposes is_amendment, amendment_type, and
        is_late_filing, and embedded HoldingActivity items also expose them.
        """
        res = self.client.get("/changes?limit=3")
        self.assertEqual(res.status_code, 200)
        items = res.json().get("items", [])
        for envelope in items:
            self.assertIn("is_amendment", envelope)
            self.assertIn("amendment_type", envelope)
            self.assertIn("is_late_filing", envelope)
            for act in envelope.get("activities", []):
                self.assertIn("is_amendment", act)
                self.assertIn("amendment_type", act)
                self.assertIn("is_late_filing", act)

    def test_r4_t3_activity_latest_v3_filing_flags(self):
        """
        [Tier 3 - Cross-Feature]
        HoldingActivity objects returned by /activity/latest/v3 expose filing flags.
        """
        res = self.client.get("/activity/latest/v3?limit=10")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        for act in activities:
            self.assertIn("is_amendment", act)
            self.assertIn("amendment_type", act)
            self.assertIn("is_late_filing", act)

    def test_r4_t3_manager_filings_endpoint_flags(self):
        """
        [Tier 4 - Cross-Feature]
        GET /managers/{cik}/filings exposes filing flags on every returned Filing item.
        """
        res = self.client.get("/managers/0001067983/filings?limit=5")
        self.assertEqual(res.status_code, 200)
        filings = res.json().get("filings", [])
        self.assertGreater(len(filings), 0)
        for f in filings:
            self.assertIn("is_amendment", f)
            self.assertIn("amendment_type", f)
            self.assertIn("is_late_filing", f)

    def test_r4_t3_filing_detail_endpoint_flags(self):
        """
        [Tier 3 - Cross-Feature]
        GET /filings/{accession_number} accurately reflects amendment and late flags.
        """
        res = self.client.get("/filings/0001085146-26-000657")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("is_amendment"))
        self.assertEqual(data.get("amendment_type"), "addition")
        self.assertTrue(data.get("is_late_filing"))

    # -------------------------------------------------------------------------
    # TIER 4: Real-World Application Scenarios (Live PostgreSQL Integration)
    # -------------------------------------------------------------------------

    def test_r4_t4_berkshire_presidents_day_on_time(self):
        """
        [Tier 4 - Real-World Integration]
        Berkshire Hathaway filing 0001193125-26-054580 filed on 2026-02-17
        (Day 48 after 2025-12-31) must be classified as on-time (is_late_filing == False)
        due to the Presidents' Day weekend roll-forward rule.
        """
        res = self.client.get("/filings/0001193125-26-054580")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["filing_date"], "2026-02-17")
        self.assertEqual(data["period_of_report"], "2025-12-31")
        self.assertIn("is_amendment", data)
        self.assertFalse(data["is_amendment"])
        self.assertIn("is_late_filing", data)
        self.assertFalse(data["is_late_filing"])

    def test_r4_t4_inspire_investing_amendment(self):
        """
        [Tier 4 - Real-World Integration]
        Inspire Investing filing 0001670139-26-000006 is a 13F-HR/A filed on 2026-05-13
        for period 2026-03-31 (on time, restatement).
        """
        res = self.client.get("/filings/0001670139-26-000006")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("is_amendment", data)
        self.assertTrue(data["is_amendment"])
        self.assertEqual(data.get("amendment_type"), "restatement")
        self.assertIn("is_late_filing", data)
        self.assertFalse(data["is_late_filing"])

    def test_r4_t4_hightower_advisors_addition_amendment(self):
        """
        [Tier 4 - Real-World Integration]
        HighTower Advisors filing 0001085146-26-000657:
        - is_amendment == True
        - amendment_type == 'addition'
        - is_late_filing == True (filed 2026-08-26 vs Aug 14 deadline)
        """
        res = self.client.get("/filings/0001085146-26-000657")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("is_amendment", data)
        self.assertTrue(data["is_amendment"])
        self.assertEqual(data.get("amendment_type"), "addition")
        self.assertIn("is_late_filing", data)
        self.assertTrue(data["is_late_filing"])

    def test_r4_t4_filings_by_aum_endpoint_flags(self):
        """
        [Tier 4 - Real-World Integration]
        GET /api/v1/search/filings_by_aum?limit=5 exposes is_amendment and is_late_filing
        on every filing item.
        """
        res = self.client.get("/api/v1/search/filings_by_aum?limit=5")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        filings = data.get("filings", [])
        self.assertGreater(len(filings), 0)
        for f in filings:
            self.assertIn("is_amendment", f)
            self.assertIn("is_late_filing", f)
            self.assertIsInstance(f["is_amendment"], bool)
            self.assertIsInstance(f["is_late_filing"], bool)


if __name__ == "__main__":
    unittest.main()
