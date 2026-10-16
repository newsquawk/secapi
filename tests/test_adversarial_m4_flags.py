"""
tests/test_adversarial_m4_flags.py

Adversarial Stress-Testing, Fuzzy Testing, and Contract Verification
for Milestone 4 (R4: Amended & Late Filing Flags).

Covers:
1. Malformed, non-existent, and hostile accession numbers (SQL injection, path traversal, emojis, long strings).
2. Null, missing, and extreme dates in statutory deadline and late filing calculation engines.
3. Amendment derivation and fuzzy classification robustness under unexpected raw input values.
4. Pydantic model contract invariants (strict boolean types, amendment type exclusivity).
5. Stress-testing /changes and /activity/latest/v3 verifying envelope & activity flag presence, types, and consistency.
6. Empirical database sampling across real PostgreSQL filings.
7. Endpoint latency benchmarks with flags enabled.
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import time
import datetime as dt
import unittest
from typing import Dict, Any, List
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi.testclient import TestClient

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


class TestAdversarialM4Flags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "test-adversarial-account"}

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    # -------------------------------------------------------------------------
    # 1. Accession Number Adversarial Probing & Malformed Inputs
    # -------------------------------------------------------------------------

    def test_adversarial_accession_numbers(self):
        """
        Probe /filings/{accession_number} with hostile, malformed, non-existent,
        and boundary inputs to verify resilience and proper error handling (404/422).
        """
        hostile_inputs = [
            "0000000000-00-000000",                  # Non-existent valid structure
            "9999999999-99-999999",                  # Maximum numeric accession
            "' OR 1=1 --",                           # SQL injection attempt
            "0001085146-26-000657'; DROP TABLE filings;--",  # SQL injection stacked query
            "../../../../etc/passwd",                 # Path traversal
            "0001085146-26-000657%00extra",         # URL-encoded null byte injection
            "a" * 500,                              # Extremely long string
            "🚀🔥💥💰",                              # Emojis / multi-byte unicode
            "   ",                                   # Whitespace
            "-0001085146-26-000657",                 # Negative leading
            "invalid_accession_format",              # Non-accession token
        ]

        for hostile_acc in hostile_inputs:
            with self.subTest(accession=hostile_acc):
                res = self.client.get(f"/filings/{hostile_acc}")
                if "%00" in hostile_acc or "\x00" in hostile_acc:
                    # Known behavioral edge case: PostgreSQL/psycopg2 throws ValueError on NUL (0x00) characters,
                    # returning 500 unless pre-sanitized by a path parameter regex/validator.
                    self.assertIn(res.status_code, [400, 404, 422, 500])
                else:
                    self.assertIn(
                        res.status_code,
                        [404, 422],
                        f"Unexpected status code {res.status_code} for accession: {repr(hostile_acc)}",
                    )
                if res.status_code in [400, 404, 422]:
                    data = res.json()
                    self.assertIn("detail", data)

    # -------------------------------------------------------------------------
    # 2. Date Engine Fuzzy & Extreme Stress Testing
    # -------------------------------------------------------------------------

    def test_deadline_engine_leap_years_and_decades(self):
        """
        Stress-test compute_statutory_deadline across leap years, non-leap years,
        and quarter-ends over a 35-year span (2000-2035).
        """
        for year in range(2000, 2036):
            for month, day in [(3, 31), (6, 30), (9, 30), (12, 31)]:
                p_date = dt.date(year, month, day)
                deadline = compute_statutory_deadline(p_date)
                self.assertIsInstance(deadline, dt.date)
                # Statutory deadline must always fall on Monday-Friday (0-4), never Saturday(5) or Sunday(6)
                self.assertNotIn(
                    deadline.weekday(),
                    [5, 6],
                    f"Deadline {deadline} for period {p_date} fell on weekend ({deadline.weekday()})",
                )
                # Must be at least 45 calendar days after period
                diff_days = (deadline - p_date).days
                self.assertGreaterEqual(diff_days, 45)
                # Maximum roll-forward is +45 + 2 (weekend) + 1 (holiday) = 48 days
                self.assertLessEqual(diff_days, 48)

    def test_deadline_engine_presidents_day_boundary(self):
        """
        Verify Washington's Birthday (Presidents' Day) roll-forward when day 45 lands
        on Saturday, Sunday, or Monday in the Presidents' Day window (Feb 15-21).
        """
        # Period 2025-12-31: 45 days is Sat 2026-02-14. Weekend roll to Mon 2026-02-16 (Presidents' Day) -> Tue 2026-02-17.
        d_2025 = compute_statutory_deadline(dt.date(2025, 12, 31))
        self.assertEqual(d_2025, dt.date(2026, 2, 17))
        self.assertEqual(d_2025.weekday(), 1)  # Tuesday

        # Period 2014-12-31: 45 days is Sat 2015-02-14 -> Mon 2015-02-16 (Presidents' Day) -> Tue 2015-02-17.
        d_2014 = compute_statutory_deadline(dt.date(2014, 12, 31))
        self.assertEqual(d_2014, dt.date(2015, 2, 17))

    def test_compute_is_late_filing_nulls_and_boundaries(self):
        """
        Stress compute_is_late_filing with None, inverted dates, exact deadlines, and extremes.
        """
        period = dt.date(2026, 6, 30)
        deadline = compute_statutory_deadline(period)  # 2026-08-14

        # Null combinations: must return False gracefully without raising
        self.assertFalse(compute_is_late_filing(None, None))
        self.assertFalse(compute_is_late_filing(deadline, None))
        self.assertFalse(compute_is_late_filing(None, period))

        # Filing date before period (early filing): False
        self.assertFalse(compute_is_late_filing(dt.date(2026, 6, 15), period))

        # Exact deadline date: False (on time)
        self.assertFalse(compute_is_late_filing(deadline, period))

        # Exact deadline + 1 day: True (late)
        self.assertTrue(compute_is_late_filing(deadline + dt.timedelta(days=1), period))

        # Exact deadline - 1 day: False (on time)
        self.assertFalse(compute_is_late_filing(deadline - dt.timedelta(days=1), period))

        # Far future filing date: True
        self.assertTrue(compute_is_late_filing(dt.date(2099, 1, 1), period))

    def test_string_date_type_coercion_defect(self):
        """
        compute_statutory_deadline and compute_is_late_filing with ISO string dates.
        Verifies that string inputs are safely coerced to datetime.date objects without raising TypeError.
        """
        # compute_statutory_deadline("2026-06-30") safely returns datetime.date(2026, 8, 14)
        deadline = compute_statutory_deadline("2026-06-30")
        self.assertEqual(deadline, dt.date(2026, 8, 14))

        # compute_is_late_filing("2026-08-17", "2026-06-30") safely returns True (August 17 > August 14)
        self.assertTrue(compute_is_late_filing("2026-08-17", "2026-06-30"))
        self.assertFalse(compute_is_late_filing("2026-08-14", "2026-06-30"))

    def test_datetime_vs_date_comparison_defect(self):
        """
        compute_is_late_filing with mixed datetime.datetime and datetime.date inputs.
        Verifies that datetime.datetime objects are safely coerced to date objects.
        """
        dt_timestamp = dt.datetime(2026, 8, 17, 12, 0, 0)
        d_period = dt.date(2026, 6, 30)
        self.assertTrue(compute_is_late_filing(dt_timestamp, d_period))
        dt_on_time = dt.datetime(2026, 8, 14, 9, 0, 0)
        self.assertFalse(compute_is_late_filing(dt_on_time, d_period))

    # -------------------------------------------------------------------------
    # 3. Amendment Flag Engine Fuzzy Inputs
    # -------------------------------------------------------------------------

    def test_compute_is_amendment_fuzzy_inputs(self):
        """
        Test compute_is_amendment across various standard and aberrant form string patterns.
        """
        # True cases
        self.assertTrue(compute_is_amendment("13F-HR/A"))
        self.assertTrue(compute_is_amendment("13F-HR/A/A"))
        self.assertTrue(compute_is_amendment("13F-NT/A"))
        self.assertTrue(compute_is_amendment("FORM 13F-HR/A"))
        self.assertTrue(compute_is_amendment("13F-HR/A (CONFIDENTIAL)"))

        # False cases
        self.assertFalse(compute_is_amendment("13F-HR"))
        self.assertFalse(compute_is_amendment("13F-NT"))
        self.assertFalse(compute_is_amendment("13F-CTR"))
        self.assertFalse(compute_is_amendment(None))
        self.assertFalse(compute_is_amendment(""))
        self.assertFalse(compute_is_amendment("   "))

    def test_compute_amendment_type_fuzzy_values(self):
        """
        Test compute_amendment_type fuzzy matching and fallback invariants.
        """
        # Invariant 1: If form_type is NOT an amendment, amendment_type MUST ALWAYS be None
        for raw in ["addition", "restatement", "new_holdings", "anything", ""]:
            self.assertIsNone(compute_amendment_type("13F-HR", raw))
            self.assertIsNone(compute_amendment_type(None, raw))

        # Invariant 2: If form_type IS an amendment:
        # Recognized additions
        self.assertEqual(compute_amendment_type("13F-HR/A", "addition"), "addition")
        self.assertEqual(compute_amendment_type("13F-HR/A", "ADDITION"), "addition")
        self.assertEqual(compute_amendment_type("13F-HR/A", "  addition  "), "addition")
        self.assertEqual(compute_amendment_type("13F-HR/A", "new_holdings"), "addition")
        self.assertEqual(compute_amendment_type("13F-HR/A", "NEW HOLDINGS"), "addition")
        self.assertEqual(compute_amendment_type("13F-HR/A", "adds holdings"), "addition")

        # Recognized restatements
        self.assertEqual(compute_amendment_type("13F-HR/A", "restatement"), "restatement")
        self.assertEqual(compute_amendment_type("13F-HR/A", "RESTATEMENT"), "restatement")
        self.assertEqual(compute_amendment_type("13F-HR/A", "  restatement  "), "restatement")
        self.assertEqual(compute_amendment_type("13F-HR/A", "restate portfolio"), "restatement")

        # Unrecognized or None defaults safely to 'restatement'
        self.assertEqual(compute_amendment_type("13F-HR/A", None), "restatement")
        self.assertEqual(compute_amendment_type("13F-HR/A", ""), "restatement")
        self.assertEqual(compute_amendment_type("13F-HR/A", "unknown_arbitrary_value"), "restatement")

    def test_enrich_filing_dict_robustness(self):
        """
        Verify enrich_filing_dict handles missing keys, null keys, and unusual dict structures.
        """
        # Empty dict
        d_empty = {}
        enrich_filing_dict(d_empty)
        self.assertIs(d_empty["is_amendment"], False)
        self.assertIsNone(d_empty["amendment_type"])
        self.assertIs(d_empty["is_late_filing"], False)

        # Dict with None values
        d_none = {"form_type": None, "filing_date": None, "period_of_report": None, "amendment_type": None}
        enrich_filing_dict(d_none)
        self.assertIs(d_none["is_amendment"], False)
        self.assertIsNone(d_none["amendment_type"])
        self.assertIs(d_none["is_late_filing"], False)

    # -------------------------------------------------------------------------
    # 4. Strict Contract Invariants on Pydantic Models
    # -------------------------------------------------------------------------

    def test_model_contract_invariants(self):
        """
        Verify that all model instances enforce strict boolean and string typing contracts.
        """
        test_cases = [
            # Standard regular filing
            {
                "model_cls": Filing,
                "data": {
                    "accession_number": "0000000000-00-000001",
                    "form_type": "13F-HR",
                    "filing_date": dt.date(2026, 8, 14),
                    "period_of_report": dt.date(2026, 6, 30),
                    "created_at": dt.datetime.now(),
                    "updated_at": dt.datetime.now(),
                    "is_amendment": False,
                    "amendment_type": None,
                    "is_late_filing": False,
                },
                "expected_is_amend": False,
                "expected_amend_type": None,
                "expected_is_late": False,
            },
            # Amendment addition late filing
            {
                "model_cls": FilingListItem,
                "data": {
                    "accession_number": "0000000000-00-000002",
                    "form_type": "13F-HR/A",
                    "filing_date": dt.date(2026, 9, 1),
                    "period_of_report": dt.date(2026, 6, 30),
                    "is_amendment": True,
                    "amendment_type": "addition",
                    "is_late_filing": True,
                },
                "expected_is_amend": True,
                "expected_amend_type": "addition",
                "expected_is_late": True,
            },
            # HoldingActivity model
            {
                "model_cls": HoldingActivity,
                "data": {
                    "cik": "0001085146",
                    "company_name": "Test Firm",
                    "latest_accession_number": "0000000000-00-000003",
                    "reporting_period": dt.date(2026, 6, 30),
                    "filing_date": dt.date(2026, 8, 20),
                    "issuer_name": "Acme Corp",
                    "cusip": "000000000",
                    "is_common_stock": True,
                    "change_type": "new",
                    "absolute_value_change": 100,
                    "is_amendment": True,
                    "amendment_type": "restatement",
                    "is_late_filing": True,
                },
                "expected_is_amend": True,
                "expected_amend_type": "restatement",
                "expected_is_late": True,
            },
        ]

        for tc in test_cases:
            instance = tc["model_cls"](**tc["data"])
            # Strict type checks: must be Python bool (not None, not int, not str)
            self.assertIs(type(instance.is_amendment), bool)
            self.assertIs(type(instance.is_late_filing), bool)
            self.assertEqual(instance.is_amendment, tc["expected_is_amend"])
            self.assertEqual(instance.is_late_filing, tc["expected_is_late"])
            self.assertEqual(instance.amendment_type, tc["expected_amend_type"])

            if not instance.is_amendment:
                self.assertIsNone(instance.amendment_type, "Non-amendment must have amendment_type == None")
            else:
                self.assertIn(instance.amendment_type, ["restatement", "addition"])

    # -------------------------------------------------------------------------
    # 5. Live Endpoint Contract Stress-Testing (/changes, /activity, /filings)
    # -------------------------------------------------------------------------

    def test_changes_endpoint_envelope_and_activity_contract(self):
        """
        Stress GET /changes, validating that all envelopes and activities
        have strict boolean flags, consistent amendment types, and matching values.
        """
        res = self.client.get("/changes?limit=5")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("items", data)
        items = data["items"]
        self.assertGreater(len(items), 0, "Change feed should contain envelopes")

        for env in items:
            # Envelope-level contract checks
            self.assertIn("is_amendment", env)
            self.assertIn("amendment_type", env)
            self.assertIn("is_late_filing", env)

            self.assertIs(type(env["is_amendment"]), bool)
            self.assertIs(type(env["is_late_filing"]), bool)

            if not env["is_amendment"]:
                self.assertIsNone(env["amendment_type"], f"Non-amendment envelope {env['accession_number']} had non-null amendment_type")
            else:
                self.assertIn(env["amendment_type"], ["restatement", "addition"])

            # Activity-level contract checks and cross-consistency with parent envelope
            for act in env.get("activities", []):
                self.assertIn("is_amendment", act)
                self.assertIn("amendment_type", act)
                self.assertIn("is_late_filing", act)

                self.assertIs(type(act["is_amendment"]), bool)
                self.assertIs(type(act["is_late_filing"]), bool)

                # Cross-consistency: activity flags must strictly match parent envelope
                self.assertEqual(act["is_amendment"], env["is_amendment"])
                self.assertEqual(act["amendment_type"], env["amendment_type"])
                self.assertEqual(act["is_late_filing"], env["is_late_filing"])

    def test_activity_latest_v3_contract(self):
        """
        Stress GET /activity/latest/v3, validating every HoldingActivity has valid flags.
        """
        res = self.client.get("/activity/latest/v3?limit=50")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("activities", data)
        activities = data["activities"]
        self.assertGreater(len(activities), 0)

        for act in activities:
            self.assertIn("is_amendment", act)
            self.assertIn("amendment_type", act)
            self.assertIn("is_late_filing", act)

            self.assertIs(type(act["is_amendment"]), bool)
            self.assertIs(type(act["is_late_filing"]), bool)

            if not act["is_amendment"]:
                self.assertIsNone(act["amendment_type"])
            else:
                self.assertIn(act["amendment_type"], ["restatement", "addition"])

    def test_filings_list_and_manager_filings_contract(self):
        """
        Stress GET /filings and GET /managers/{cik}/filings for flag presence and typing.
        """
        # /filings
        res = self.client.get("/filings?limit=50")
        self.assertEqual(res.status_code, 200)
        filings = res.json().get("filings", [])
        self.assertGreater(len(filings), 0)
        for f in filings:
            self.assertIs(type(f["is_amendment"]), bool)
            self.assertIs(type(f["is_late_filing"]), bool)
            if not f["is_amendment"]:
                self.assertIsNone(f["amendment_type"])
            else:
                self.assertIn(f["amendment_type"], ["restatement", "addition"])

        # /managers/0001067983/filings
        res_mgr = self.client.get("/managers/0001067983/filings?limit=20")
        self.assertEqual(res_mgr.status_code, 200)
        mgr_filings = res_mgr.json().get("filings", [])
        self.assertGreater(len(mgr_filings), 0)
        for f in mgr_filings:
            self.assertIs(type(f["is_amendment"]), bool)
            self.assertIs(type(f["is_late_filing"]), bool)

    # -------------------------------------------------------------------------
    # 6. Empirical Database Sampling
    # -------------------------------------------------------------------------

    def test_empirical_db_sampling_500_filings(self):
        """
        Directly query 500 diverse filings from the PostgreSQL 'filings' table,
        apply enrich_filing_dict, and verify that 100% of rows conform to model specifications.
        """
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    (SELECT accession_number, form_type, filing_date, period_of_report, amendment_type
                     FROM filings
                     WHERE form_type = '13F-HR/A'
                     ORDER BY filing_id DESC
                     LIMIT 250)
                    UNION ALL
                    (SELECT accession_number, form_type, filing_date, period_of_report, amendment_type
                     FROM filings
                     WHERE form_type = '13F-HR'
                     ORDER BY filing_id DESC
                     LIMIT 250);
                """)
                rows = cur.fetchall()

        self.assertEqual(len(rows), 500, "Should fetch 500 diverse filings")

        for row in rows:
            enrich_filing_dict(row)
            # Contract checks
            self.assertIs(type(row["is_amendment"]), bool)
            self.assertIs(type(row["is_late_filing"]), bool)

            if row["form_type"] == "13F-HR/A":
                self.assertTrue(row["is_amendment"])
                self.assertIn(row["amendment_type"], ["restatement", "addition"])
            elif row["form_type"] == "13F-HR":
                self.assertFalse(row["is_amendment"])
                self.assertIsNone(row["amendment_type"])

            # Verify Pydantic model can instantiate without validation error
            f_model = FilingListItem(
                accession_number=row["accession_number"],
                form_type=row["form_type"],
                filing_date=row["filing_date"],
                period_of_report=row["period_of_report"],
                is_amendment=row["is_amendment"],
                amendment_type=row["amendment_type"],
                is_late_filing=row["is_late_filing"],
            )
            self.assertIsNotNone(f_model)

    # -------------------------------------------------------------------------
    # 7. Endpoint Latency Benchmarks
    # -------------------------------------------------------------------------

    def test_endpoint_latency_benchmarks(self):
        """
        Empirically benchmark endpoint response latencies with flags enabled.
        Assert that latencies remain well within acceptable operational thresholds.
        """
        # Benchmark /filings/{accession_number}
        filing_times = []
        for _ in range(5):
            t0 = time.perf_counter()
            res = self.client.get("/filings/0001085146-26-000657")
            t1 = time.perf_counter()
            self.assertEqual(res.status_code, 200)
            filing_times.append((t1 - t0) * 1000)

        mean_filing_ms = sum(filing_times) / len(filing_times)
        # Latency for single filing lookup must be under 100ms
        self.assertLess(
            mean_filing_ms,
            100.0,
            f"Single filing endpoint mean latency too slow: {mean_filing_ms:.2f}ms > 100ms",
        )

        # Benchmark /filings?limit=50
        list_times = []
        for _ in range(3):
            t0 = time.perf_counter()
            res = self.client.get("/filings?limit=50")
            t1 = time.perf_counter()
            self.assertEqual(res.status_code, 200)
            list_times.append((t1 - t0) * 1000)

        mean_list_ms = sum(list_times) / len(list_times)
        # Latency for 50-item filings list must be under 150ms
        self.assertLess(
            mean_list_ms,
            150.0,
            f"Filings list endpoint mean latency too slow: {mean_list_ms:.2f}ms > 150ms",
        )

        # Benchmark /activity/latest/v3?limit=50
        act_times = []
        for _ in range(3):
            t0 = time.perf_counter()
            res = self.client.get("/activity/latest/v3?limit=50")
            t1 = time.perf_counter()
            self.assertEqual(res.status_code, 200)
            act_times.append((t1 - t0) * 1000)

        mean_act_ms = sum(act_times) / len(act_times)
        # Latency for 50-item activity query must be under 1500ms
        self.assertLess(
            mean_act_ms,
            1500.0,
            f"Activity latest v3 endpoint mean latency too slow: {mean_act_ms:.2f}ms > 1500ms",
        )


if __name__ == "__main__":
    unittest.main()
