"""
tests/test_challenger_m4.py

Empirical Challenger Test Suite for Milestone 4 (R4: Amended and Late Filing Flags).
Authored by challenger_m4_1.

Adversarial Challenge Dimensions:
1. Exhaustive statutory deadline calendar calculations across multiple decades (2015-2035)
   verifying all 84 quarter ends, leap years, Saturday/Sunday deadlines, and Presidents' Day roll-forwards.
2. Property-based invariants: deadlines never fall on weekends or federal holidays (Presidents' Day).
3. Live database queries verifying schema, indexes, and full dataset integrity across 289,013 filings
   (271,349 13F-HR and 17,664 13F-HR/A).
4. Strict classification accuracy of amendment_type ('restatement' vs 'addition' vs None).
5. Comprehensive end-to-end API endpoint verification across /filings, /filings/{accession},
   /api/v1/search/filings_by_aum, /managers/{cik}/filings, /changes, /activity/latest/v3, and /activity/latest/options.
6. Pydantic model contract validation and schema integrity.
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import datetime as dt
import unittest
from typing import Dict, Any, List, Optional
from fastapi.testclient import TestClient
import psycopg2
from psycopg2.extras import RealDictCursor

from main import app
from auth import sync_dependencies
from sec_models import (
    Filing,
    FilingListItem,
    FilingDetail,
    FilingEnvelope,
    HoldingActivity,
    compute_statutory_deadline,
    compute_is_late_filing,
    compute_is_amendment,
    compute_amendment_type,
    enrich_filing_dict,
)


def get_db_connection():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME", "sec"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "password"),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
    )


def independent_statutory_deadline_oracle(period_of_report: dt.date) -> dt.date:
    """
    Independent ground-truth oracle for SEC Rule 13f-1(a)(1) & Rule 0-3(a):
    1. Base: period_of_report + 45 calendar days.
    2. Weekend roll-forward:
       - Saturday (weekday 5) -> +2 days to Monday.
       - Sunday (weekday 6) -> +1 day to Monday.
    3. Federal Holiday roll-forward:
       - Washington's Birthday (Presidents' Day): Third Monday in February (days 15-21) -> +1 day to Tuesday.
    """
    deadline = period_of_report + dt.timedelta(days=45)
    if deadline.weekday() == 5:
        deadline += dt.timedelta(days=2)
    elif deadline.weekday() == 6:
        deadline += dt.timedelta(days=1)

    # Check Presidents' Day
    if deadline.month == 2 and deadline.weekday() == 0 and 15 <= deadline.day <= 21:
        deadline += dt.timedelta(days=1)

    return deadline


class TestChallengerStatutoryDeadlines(unittest.TestCase):
    """Adversarial stress-testing of statutory deadline calendar logic."""

    def test_exhaustive_quarter_deadlines_2015_to_2035(self):
        """
        [Exhaustive Calendar Stress Test]
        Verify all 84 calendar quarters across 21 years (2015-2035 inclusive):
        - Q1 (03-31), Q2 (06-30), Q3 (09-30), Q4 (12-31).
        - Assert that implementation matches the independent oracle exactly.
        - Assert that no deadline ever lands on Saturday or Sunday (weekday < 5).
        - Assert that no deadline ever lands on Presidents' Day.
        """
        quarter_ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
        tested_count = 0

        for year in range(2015, 2036):
            for month, day in quarter_ends:
                period = dt.date(year, month, day)
                impl_deadline = compute_statutory_deadline(period)
                oracle_deadline = independent_statutory_deadline_oracle(period)

                self.assertEqual(
                    impl_deadline,
                    oracle_deadline,
                    f"Mismatch for period {period}: impl={impl_deadline}, oracle={oracle_deadline}",
                )

                # Invariant 1: Must be a business day (Monday through Friday)
                self.assertLess(
                    impl_deadline.weekday(),
                    5,
                    f"Deadline fell on weekend for period {period}: {impl_deadline} (weekday={impl_deadline.weekday()})",
                )

                # Invariant 2: Cannot be Washington's Birthday (Presidents' Day)
                if impl_deadline.month == 2:
                    is_presidents_day = (impl_deadline.weekday() == 0 and 15 <= impl_deadline.day <= 21)
                    self.assertFalse(
                        is_presidents_day,
                        f"Deadline fell on Presidents' Day for period {period}: {impl_deadline}",
                    )

                tested_count += 1

        self.assertEqual(tested_count, 21 * 4, f"Expected 84 quarters, tested {tested_count}")

    def test_presidents_day_roll_forward_known_years(self):
        """
        [Boundary & Holiday Test]
        Verify known years where December 31 + 45 days lands on Saturday Feb 14 or Sunday Feb 15,
        triggering a roll forward across the Presidents' Day Monday to Tuesday.
        """
        known_cases = [
            # 2014-12-31 + 45 = Sat 2015-02-14 -> Mon Feb 16 (Presidents' Day) -> Tue Feb 17
            (dt.date(2014, 12, 31), dt.date(2015, 2, 17)),
            # 2020-12-31 + 45 = Sun 2021-02-14 -> Mon Feb 15 (Presidents' Day) -> Tue Feb 16
            (dt.date(2020, 12, 31), dt.date(2021, 2, 16)),
            # 2025-12-31 + 45 = Sat 2026-02-14 -> Mon Feb 16 (Presidents' Day) -> Tue Feb 17
            (dt.date(2025, 12, 31), dt.date(2026, 2, 17)),
            # 2026-12-31 + 45 = Sun 2027-02-14 -> Mon Feb 15 (Presidents' Day) -> Tue Feb 16
            (dt.date(2026, 12, 31), dt.date(2027, 2, 16)),
            # 2031-12-31 + 45 = Sat 2032-02-14 -> Mon Feb 16 (Presidents' Day) -> Tue Feb 17
            (dt.date(2031, 12, 31), dt.date(2032, 2, 17)),
        ]

        for period, expected_deadline in known_cases:
            computed = compute_statutory_deadline(period)
            self.assertEqual(
                computed,
                expected_deadline,
                f"Failed for period {period}: expected {expected_deadline}, got {computed}",
            )
            # Must be Tuesday (weekday 1)
            self.assertEqual(computed.weekday(), 1)

    def test_saturday_roll_forward_across_quarters(self):
        """
        [Boundary Test]
        Verify Saturday roll-forwards (+2 days to Monday) across Q1, Q2, Q3.
        """
        # Q1: March 31 + 45 days = May 15. In 2021, May 15 was Saturday -> May 17 Monday.
        self.assertEqual(compute_statutory_deadline(dt.date(2021, 3, 31)), dt.date(2021, 5, 17))
        # Q2: June 30 + 45 days = August 14. In 2021, Aug 14 was Saturday -> Aug 16 Monday.
        self.assertEqual(compute_statutory_deadline(dt.date(2021, 6, 30)), dt.date(2021, 8, 16))
        # Q3: September 30 + 45 days = November 14. In 2020, Nov 14 was Saturday -> Nov 16 Monday.
        self.assertEqual(compute_statutory_deadline(dt.date(2020, 9, 30)), dt.date(2020, 11, 16))
        # Q3 2026: Nov 14 is Saturday -> Nov 16 Monday.
        self.assertEqual(compute_statutory_deadline(dt.date(2026, 9, 30)), dt.date(2026, 11, 16))

    def test_sunday_roll_forward_across_quarters(self):
        """
        [Boundary Test]
        Verify Sunday roll-forwards (+1 day to Monday) across Q1, Q2, Q3.
        """
        # Q1: March 31 + 45 days = May 15. In 2022, May 15 was Sunday -> May 16 Monday.
        self.assertEqual(compute_statutory_deadline(dt.date(2022, 3, 31)), dt.date(2022, 5, 16))
        # Q2: June 30 + 45 days = August 14. In 2022, Aug 14 was Sunday -> Aug 15 Monday.
        self.assertEqual(compute_statutory_deadline(dt.date(2022, 6, 30)), dt.date(2022, 8, 15))
        # Q3: September 30 + 45 days = November 14. In 2021, Nov 14 was Sunday -> Nov 15 Monday.
        self.assertEqual(compute_statutory_deadline(dt.date(2021, 9, 30)), dt.date(2021, 11, 15))
        # Q3 2027: Nov 14 is Sunday -> Nov 15 Monday.
        self.assertEqual(compute_statutory_deadline(dt.date(2027, 9, 30)), dt.date(2027, 11, 15))

    def test_leap_years_quarter_and_off_quarter_invariance(self):
        """
        [Leap Year Stress Test]
        Verify leap years (2016, 2020, 2024, 2028, 2032, 2036).
        Dec 31 + 45 is always Feb 14 regardless of leap year because Feb 14 is before Feb 29.
        Also tests off-quarter dates crossing Feb 29.
        """
        leap_years = [2016, 2020, 2024, 2028, 2032]
        for yr in leap_years:
            q4_deadline = compute_statutory_deadline(dt.date(yr - 1, 12, 31))
            self.assertIn(q4_deadline.month, [2])
            self.assertIn(q4_deadline.day, [14, 15, 16, 17])

        # Period ending on leap day 2024-02-29 + 45 days = 2024-04-14 (Sunday) -> rolls to 2024-04-15 (Monday)
        self.assertEqual(compute_statutory_deadline(dt.date(2024, 2, 29)), dt.date(2024, 4, 15))

    def test_compute_is_late_filing_boundaries_and_nulls(self):
        """
        [Boundary & Null Handling Test]
        Verify exact deadline, day before, day after, and null edge cases.
        """
        period = dt.date(2026, 6, 30)
        deadline = compute_statutory_deadline(period)  # 2026-08-14 (Friday)
        self.assertEqual(deadline, dt.date(2026, 8, 14))

        # Exactly on deadline -> False (not late)
        self.assertFalse(compute_is_late_filing(deadline, period))
        # 1 day before -> False
        self.assertFalse(compute_is_late_filing(deadline - dt.timedelta(days=1), period))
        # 10 days before -> False
        self.assertFalse(compute_is_late_filing(deadline - dt.timedelta(days=10), period))
        # 1 day after -> True (late!)
        self.assertTrue(compute_is_late_filing(deadline + dt.timedelta(days=1), period))
        # 100 days after -> True (late!)
        self.assertTrue(compute_is_late_filing(deadline + dt.timedelta(days=100), period))

        # Missing values must safely return False
        self.assertFalse(compute_is_late_filing(None, period))
        self.assertFalse(compute_is_late_filing(deadline, None))
        self.assertFalse(compute_is_late_filing(None, None))


class TestChallengerAmendmentClassification(unittest.TestCase):
    """Adversarial stress-testing of amendment classification accuracy."""

    def test_compute_is_amendment_comprehensive(self):
        """
        [Form Type Invariance Test]
        Only forms containing '/A' qualify as amendments.
        """
        # Non-amendment forms
        for form in ["13F-HR", "13F-NT", "13F-CTR", "10-K", "10-Q", "8-K", "", None]:
            self.assertFalse(
                compute_is_amendment(form),
                f"Expected False for form {form}",
            )

        # Amendment forms
        for form in ["13F-HR/A", "13F-HR/A/A", "13F-NT/A", "10-K/A", "SCHEDULE 13D/A"]:
            self.assertTrue(
                compute_is_amendment(form),
                f"Expected True for form {form}",
            )

    def test_compute_amendment_type_semantics_and_fallbacks(self):
        """
        [Amendment Type Normalization Test]
        Verify restatement vs addition normalization and strict nullability on non-amendments.
        """
        # Invariant 1: Non-amendments MUST ALWAYS have amendment_type == None
        for non_amend in ["13F-HR", "13F-NT", None, ""]:
            for raw in [None, "addition", "restatement", "new_holdings", "anything"]:
                self.assertIsNone(
                    compute_amendment_type(non_amend, raw),
                    f"Non-amendment {non_amend} with raw={raw} returned non-None amendment_type!",
                )

        # Invariant 2: Amendments classify 'addition' variations
        for add_variant in ["addition", "Addition", "ADDITION", "new_holdings", "add"]:
            self.assertEqual(
                compute_amendment_type("13F-HR/A", add_variant),
                "addition",
            )

        # Invariant 3: Amendments classify 'restatement' variations
        for restate_variant in ["restatement", "Restatement", "RESTATEMENT", "restate"]:
            self.assertEqual(
                compute_amendment_type("13F-HR/A", restate_variant),
                "restatement",
            )

        # Invariant 4: Amendments default to 'restatement' if unclassified or None
        self.assertEqual(compute_amendment_type("13F-HR/A", None), "restatement")
        self.assertEqual(compute_amendment_type("13F-HR/A", ""), "restatement")

    def test_enrich_filing_dict_mutation_adversarial(self):
        """
        [Dictionary Mutation Invariance Test]
        Verify enrich_filing_dict enriches in-place without corrupting existing fields.
        """
        d = {
            "accession_number": "000-000",
            "company_name": "Test Capital",
            "form_type": "13F-HR",
            "filing_date": dt.date(2026, 8, 14),
            "period_of_report": dt.date(2026, 6, 30),
            "custom_payload": 12345,
        }
        res = enrich_filing_dict(d)
        self.assertIs(res, d, "enrich_filing_dict must return the same dictionary reference")
        self.assertEqual(d["custom_payload"], 12345)
        self.assertFalse(d["is_amendment"])
        self.assertIsNone(d["amendment_type"])
        self.assertFalse(d["is_late_filing"])

        d2 = {
            "form_type": "13F-HR/A",
            "filing_date": dt.date(2026, 8, 20),
            "period_of_report": dt.date(2026, 6, 30),
            "amendment_type": "addition",
        }
        enrich_filing_dict(d2)
        self.assertTrue(d2["is_amendment"])
        self.assertEqual(d2["amendment_type"], "addition")
        self.assertTrue(d2["is_late_filing"])


class TestChallengerLiveDatabaseIntegrity(unittest.TestCase):
    """Empirical verification against the real PostgreSQL sec database."""

    def test_database_schema_and_column_indexing(self):
        """
        [DB Schema Verification]
        Verify column amendment_type exists on table filings and index idx_filings_amendment_type exists.
        """
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name, data_type 
                    FROM information_schema.columns 
                    WHERE table_name = 'filings' AND column_name = 'amendment_type';
                """)
                col = cur.fetchone()
                self.assertIsNotNone(col, "Column amendment_type does not exist on table filings!")
                self.assertEqual(col[0], "amendment_type")

                cur.execute("""
                    SELECT indexname FROM pg_indexes 
                    WHERE tablename = 'filings' AND indexname = 'idx_filings_amendment_type';
                """)
                idx = cur.fetchone()
                self.assertIsNotNone(idx, "Index idx_filings_amendment_type does not exist!")

    def test_database_population_invariants(self):
        """
        [DB Data Integrity Verification]
        Across all 289,013 filings:
        - 100% of 13F-HR filings have amendment_type IS NULL.
        - 100% of 13F-HR/A filings have amendment_type IN ('restatement', 'addition').
        """
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Count standard filings
                cur.execute("SELECT count(*) FROM filings WHERE form_type = '13F-HR';")
                cnt_standard = cur.fetchone()[0]
                self.assertEqual(cnt_standard, 271349)

                # Standard filings must NEVER have amendment_type populated
                cur.execute("SELECT count(*) FROM filings WHERE form_type = '13F-HR' AND amendment_type IS NOT NULL;")
                cnt_standard_violating = cur.fetchone()[0]
                self.assertEqual(cnt_standard_violating, 0, "Standard 13F-HR filings must have amendment_type IS NULL")

                # Count amendment filings
                cur.execute("SELECT count(*) FROM filings WHERE form_type LIKE '%/A%';")
                cnt_amend = cur.fetchone()[0]
                self.assertEqual(cnt_amend, 17664)

                # All amendment filings MUST have amendment_type populated
                cur.execute("SELECT count(*) FROM filings WHERE form_type LIKE '%/A%' AND amendment_type IS NULL;")
                cnt_amend_null = cur.fetchone()[0]
                self.assertEqual(cnt_amend_null, 0, "All 13F-HR/A filings must have non-null amendment_type")

                # Check breakdown
                cur.execute("SELECT amendment_type, count(*) FROM filings WHERE form_type LIKE '%/A%' GROUP BY amendment_type;")
                breakdown = dict(cur.fetchall())
                self.assertEqual(set(breakdown.keys()), {"addition", "restatement"})
                self.assertEqual(breakdown["addition"], 2755)
                self.assertEqual(breakdown["restatement"], 14909)

    def test_addition_vs_restatement_holding_volume_oracle(self):
        """
        [Empirical Holding Volume Verification]
        Confirm that 'addition' amendments represent partial updates with significantly fewer
        holdings than the base filing, whereas 'restatements' replace the entire portfolio.
        """
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # HighTower Advisors addition 0001085146-26-000657
                cur.execute("""
                    SELECT f.accession_number, f.amendment_type, count(h.holding_id) as holding_count
                    FROM filings f
                    LEFT JOIN holdings h ON f.filing_id = h.filing_id
                    WHERE f.accession_number = '0001085146-26-000657'
                    GROUP BY f.accession_number, f.amendment_type;
                """)
                row_add = cur.fetchone()
                self.assertIsNotNone(row_add)
                self.assertEqual(row_add["amendment_type"], "addition")
                self.assertEqual(row_add["holding_count"], 1)

                # Base filing for same company and period
                cur.execute("""
                    SELECT f.accession_number, f.form_type, count(h.holding_id) as holding_count
                    FROM filings f
                    LEFT JOIN holdings h ON f.filing_id = h.filing_id
                    WHERE f.company_id = (SELECT company_id FROM filings WHERE accession_number = '0001085146-26-000657')
                      AND f.period_of_report = '2026-06-30'
                      AND f.form_type = '13F-HR'
                    GROUP BY f.accession_number, f.form_type;
                """)
                row_base = cur.fetchone()
                self.assertIsNotNone(row_base)
                self.assertGreater(row_base["holding_count"], 1000, "Base filing should have extensive holdings (> 1000)")

    def test_berkshire_hathaway_presidents_day_empirical_filing(self):
        """
        [Real World Ground Truth Verification]
        Berkshire Hathaway filing 0001193125-26-054580:
        - period_of_report: 2025-12-31
        - filing_date: 2026-02-17 (Day 48 after quarter-end)
        - Day 45 (Feb 14) was Saturday -> Sunday Feb 15 -> Monday Feb 16 (Presidents' Day) -> Tuesday Feb 17
        - Empirical outcome: must be on-time (is_late_filing == False).
        """
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT accession_number, form_type, filing_date, period_of_report, amendment_type
                    FROM filings
                    WHERE accession_number = '0001193125-26-054580';
                """)
                row = cur.fetchone()
                self.assertIsNotNone(row)
                enrich_filing_dict(row)
                self.assertFalse(row["is_amendment"])
                self.assertIsNone(row["amendment_type"])
                self.assertFalse(row["is_late_filing"], "Berkshire filed on Feb 17 (Presidents' Day rolled deadline); MUST NOT be late!")


class TestChallengerEndpointIntegration(unittest.TestCase):
    """End-to-end HTTP API endpoint contract and consistency testing."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "test-service-account"}

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    def test_endpoint_filings_list_consistency(self):
        """GET /filings returns items with strictly typed R4 flags."""
        res = self.client.get("/filings?limit=25")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        filings = data.get("filings", [])
        self.assertGreater(len(filings), 0)

        for f in filings:
            self.assertIn("is_amendment", f)
            self.assertIn("amendment_type", f)
            self.assertIn("is_late_filing", f)
            self.assertIsInstance(f["is_amendment"], bool)
            self.assertIsInstance(f["is_late_filing"], bool)

            if not f["is_amendment"]:
                self.assertIsNone(
                    f["amendment_type"],
                    f"Non-amendment filing {f['accession_number']} had amendment_type={f['amendment_type']}",
                )
            else:
                self.assertIn(
                    f["amendment_type"],
                    ["restatement", "addition"],
                    f"Amendment filing {f['accession_number']} had unexpected amendment_type={f['amendment_type']}",
                )

    def test_endpoint_search_filings_by_aum_flags(self):
        """GET /api/v1/search/filings_by_aum exposes is_amendment and is_late_filing."""
        res = self.client.get("/api/v1/search/filings_by_aum?limit=20")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        filings = data.get("filings", [])
        self.assertGreater(len(filings), 0)

        for f in filings:
            self.assertIn("is_amendment", f)
            self.assertIn("is_late_filing", f)
            self.assertIsInstance(f["is_amendment"], bool)
            self.assertIsInstance(f["is_late_filing"], bool)

    def test_endpoint_manager_filings(self):
        """GET /managers/{cik}/filings exposes R4 flags for manager filings."""
        # Berkshire Hathaway CIK 0001067983
        res = self.client.get("/managers/0001067983/filings?limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        filings = data.get("filings", [])
        self.assertGreater(len(filings), 0)

        for f in filings:
            self.assertIn("is_amendment", f)
            self.assertIn("amendment_type", f)
            self.assertIn("is_late_filing", f)
            self.assertIsInstance(f["is_amendment"], bool)
            self.assertIsInstance(f["is_late_filing"], bool)

    def test_endpoint_changes_envelope_and_activity_consistency(self):
        """GET /changes ensures envelopes and activities have identical filing flags."""
        res = self.client.get("/changes?limit=5")
        self.assertEqual(res.status_code, 200)
        items = res.json().get("items", [])
        self.assertGreater(len(items), 0)

        for env in items:
            self.assertIn("is_amendment", env)
            self.assertIn("amendment_type", env)
            self.assertIn("is_late_filing", env)

            for act in env.get("activities", []):
                self.assertEqual(
                    act["is_amendment"],
                    env["is_amendment"],
                    "HoldingActivity.is_amendment must match FilingEnvelope.is_amendment",
                )
                self.assertEqual(
                    act["amendment_type"],
                    env["amendment_type"],
                    "HoldingActivity.amendment_type must match FilingEnvelope.amendment_type",
                )
                self.assertEqual(
                    act["is_late_filing"],
                    env["is_late_filing"],
                    "HoldingActivity.is_late_filing must match FilingEnvelope.is_late_filing",
                )

    def test_endpoint_activity_latest_v3_and_options(self):
        """GET /activity/latest/v3 and /activity/latest/options expose R4 flags."""
        for endpoint in ["/activity/latest/v3?limit=10", "/activity/latest/options?limit=5"]:
            res = self.client.get(endpoint)
            self.assertEqual(res.status_code, 200)
            activities = res.json().get("activities", [])
            for act in activities:
                self.assertIn("is_amendment", act)
                self.assertIn("amendment_type", act)
                self.assertIn("is_late_filing", act)
                self.assertIsInstance(act["is_amendment"], bool)
                self.assertIsInstance(act["is_late_filing"], bool)


class TestChallengerModelContracts(unittest.TestCase):
    """Adversarial stress-testing of Pydantic model schemas and defaults."""

    def test_pydantic_models_field_declarations_and_serialization(self):
        """Verify all target Pydantic models define R4 fields with expected defaults."""
        # 1. Filing
        f = Filing(
            accession_number="000-01",
            form_type="13F-HR",
            filing_date=dt.date(2026, 8, 14),
            period_of_report=dt.date(2026, 6, 30),
            created_at=dt.datetime.now(),
            updated_at=dt.datetime.now(),
        )
        self.assertFalse(f.is_amendment)
        self.assertIsNone(f.amendment_type)
        self.assertFalse(f.is_late_filing)

        # 2. FilingListItem
        fli = FilingListItem(
            accession_number="000-02",
            form_type="13F-HR",
            filing_date=dt.date(2026, 8, 14),
            period_of_report=dt.date(2026, 6, 30),
        )
        self.assertFalse(fli.is_amendment)
        self.assertIsNone(fli.amendment_type)
        self.assertFalse(fli.is_late_filing)

        # 3. FilingDetail
        fd = FilingDetail(
            accession_number="000-03",
            form_type="13F-HR",
            filing_date=dt.date(2026, 8, 14),
            period_of_report=dt.date(2026, 6, 30),
        )
        self.assertFalse(fd.is_amendment)
        self.assertIsNone(fd.amendment_type)
        self.assertFalse(fd.is_late_filing)

        # 4. FilingEnvelope
        fe = FilingEnvelope(
            filing_id=10,
            accession_number="000-04",
            cik="0001234567",
            company_name="Apex Global",
            form_type="13F-HR",
            filing_date=dt.date(2026, 8, 14),
            period_of_report=dt.date(2026, 6, 30),
        )
        self.assertFalse(fe.is_amendment)
        self.assertIsNone(fe.amendment_type)
        self.assertFalse(fe.is_late_filing)

        # 5. HoldingActivity
        ha = HoldingActivity(
            cik="0001234567",
            company_name="Apex Global",
            latest_accession_number="000-05",
            reporting_period=dt.date(2026, 6, 30),
            filing_date=dt.date(2026, 8, 14),
            issuer_name="Microsoft Corp",
            cusip="594918104",
            is_common_stock=True,
            change_type="increased",
            absolute_value_change=500000,
            is_amendment=True,
            amendment_type="addition",
            is_late_filing=True,
        )
        ha_dict = ha.model_dump() if hasattr(ha, "model_dump") else ha.dict()
        self.assertTrue(ha_dict["is_amendment"])
        self.assertEqual(ha_dict["amendment_type"], "addition")
        self.assertTrue(ha_dict["is_late_filing"])


if __name__ == "__main__":
    unittest.main()
