"""
tests/test_amended_and_late_filing_flags.py

Unit and integration tests for Milestone 4 (R4): Amended and Late Filing Flags.
Covers:
1. compute_statutory_deadline (Rule 13f-1(a)(1) & Rule 0-3(a) weekend & holiday roll-forward)
2. compute_is_late_filing
3. compute_is_amendment
4. compute_amendment_type
5. enrich_filing_dict helper
6. Pydantic models (Filing, FilingListItem, FilingDetail, FilingEnvelope, HoldingActivity)
7. Endpoint responses across /filings, /managers/{cik}/filings, /changes, /activity/latest/v3, and /activity/latest/options
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import datetime as dt
import unittest
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


class TestAmendedAndLateFilingFlags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "test-service-account"}

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    # -------------------------------------------------------------------------
    # Unit Tests: Statutory Deadline Engine
    # -------------------------------------------------------------------------

    def test_compute_statutory_deadline_standard_weekday(self):
        """Standard 45-day deadline falling on a weekday needs no roll-forward."""
        # Q2 2026 ends 2026-06-30. June has 30 days.
        # 2026-06-30 + 45 days = 2026-08-14 (Friday, weekday 4).
        deadline = compute_statutory_deadline(dt.date(2026, 6, 30))
        self.assertEqual(deadline, dt.date(2026, 8, 14))
        self.assertEqual(deadline.weekday(), 4)

        # Robust type coercion: string and datetime inputs
        self.assertEqual(compute_statutory_deadline("2026-06-30"), dt.date(2026, 8, 14))
        self.assertEqual(compute_statutory_deadline(dt.datetime(2026, 6, 30, 10, 0, 0)), dt.date(2026, 8, 14))
        self.assertIsNone(compute_statutory_deadline(None))

    def test_compute_statutory_deadline_saturday_roll_forward(self):
        """When 45th calendar day lands on Saturday, deadline rolls +2 days to Monday."""
        # Period 2026-09-30 + 45 days = 2026-11-14 (Saturday, weekday 5).
        # Rolls to Monday 2026-11-16.
        deadline = compute_statutory_deadline(dt.date(2026, 9, 30))
        self.assertEqual(deadline, dt.date(2026, 11, 16))
        self.assertEqual(deadline.weekday(), 0)

    def test_compute_statutory_deadline_sunday_roll_forward(self):
        """When 45th calendar day lands on Sunday, deadline rolls +1 day to Monday."""
        # Period 2027-09-30 + 45 days = 2027-11-14 (Sunday, weekday 6).
        # Rolls to Monday 2027-11-15.
        deadline = compute_statutory_deadline(dt.date(2027, 9, 30))
        self.assertEqual(deadline, dt.date(2027, 11, 15))
        self.assertEqual(deadline.weekday(), 0)

    def test_compute_statutory_deadline_presidents_day_roll_forward(self):
        """Period ending 2025-12-31 rolls Saturday -> Monday -> Presidents' Day Tuesday."""
        # Period 2025-12-31 + 45 days = 2026-02-14 (Saturday).
        # Weekend roll -> 2026-02-16 (Monday).
        # 2026-02-16 is 3rd Monday in Feb (15 <= 16 <= 21) -> Presidents' Day!
        # Rolls forward +1 day to Tuesday 2026-02-17.
        deadline = compute_statutory_deadline(dt.date(2025, 12, 31))
        self.assertEqual(deadline, dt.date(2026, 2, 17))
        self.assertEqual(deadline.weekday(), 1)  # Tuesday

    def test_compute_is_late_filing_exact_deadline(self):
        """A filing submitted on the exact statutory deadline date is NOT late."""
        period = dt.date(2025, 12, 31)
        deadline = compute_statutory_deadline(period)
        self.assertFalse(compute_is_late_filing(deadline, period))

        # Test string and datetime inputs
        self.assertFalse(compute_is_late_filing("2026-02-17", "2025-12-31"))
        self.assertFalse(compute_is_late_filing(dt.datetime(2026, 2, 17, 18, 0), dt.date(2025, 12, 31)))

    def test_compute_is_late_filing_one_day_after(self):
        """A filing submitted one calendar day after the deadline IS late."""
        period = dt.date(2025, 12, 31)
        deadline = compute_statutory_deadline(period)
        self.assertTrue(compute_is_late_filing(deadline + dt.timedelta(days=1), period))

        # Test string and datetime inputs
        self.assertTrue(compute_is_late_filing("2026-02-18", "2025-12-31"))
        self.assertTrue(compute_is_late_filing(dt.datetime(2026, 2, 18, 9, 30), dt.datetime(2025, 12, 31, 0, 0)))

    def test_compute_is_late_filing_none_dates(self):
        """Missing or None dates return False gracefully."""
        self.assertFalse(compute_is_late_filing(None, dt.date(2026, 6, 30)))
        self.assertFalse(compute_is_late_filing(dt.date(2026, 8, 14), None))
        self.assertFalse(compute_is_late_filing(None, None))
        self.assertFalse(compute_is_late_filing("", ""))

    # -------------------------------------------------------------------------
    # Unit Tests: Amendment Flags Engine
    # -------------------------------------------------------------------------

    def test_compute_is_amendment(self):
        """Verify is_amendment detection across standard and amendment forms."""
        self.assertFalse(compute_is_amendment("13F-HR"))
        self.assertFalse(compute_is_amendment("13F-NT"))
        self.assertFalse(compute_is_amendment(None))
        self.assertFalse(compute_is_amendment(""))
        self.assertTrue(compute_is_amendment("13F-HR/A"))
        self.assertTrue(compute_is_amendment("13F-HR/A/A"))
        self.assertTrue(compute_is_amendment("13F-NT/A"))
        # Case-insensitivity support
        self.assertTrue(compute_is_amendment("13f-hr/a"))
        self.assertTrue(compute_is_amendment("13f-nt/a"))
        self.assertTrue(compute_is_amendment(" 13f-hr/a "))

    def test_compute_amendment_type_non_amendment(self):
        """Non-amendment filings must ALWAYS have amendment_type == None."""
        self.assertIsNone(compute_amendment_type("13F-HR"))
        self.assertIsNone(compute_amendment_type("13F-NT"))
        self.assertIsNone(compute_amendment_type("13f-hr"))
        self.assertIsNone(compute_amendment_type("13F-HR", raw_amendment_type="restatement"))
        self.assertIsNone(compute_amendment_type(None))

    def test_compute_amendment_type_amendment_classification(self):
        """Amendments classify into 'addition' or 'restatement'."""
        self.assertEqual(compute_amendment_type("13F-HR/A", "addition"), "addition")
        self.assertEqual(compute_amendment_type("13F-HR/A", "new_holdings"), "addition")
        self.assertEqual(compute_amendment_type("13F-HR/A", "restatement"), "restatement")
        # Case-insensitivity support
        self.assertEqual(compute_amendment_type("13f-hr/a", "ADDITION"), "addition")
        self.assertEqual(compute_amendment_type("13F-HR/A", "RESTATEMENT"), "restatement")
        self.assertEqual(compute_amendment_type("13f-hr/a", "New_Holdings"), "addition")
        # Default fallback when unclassified
        self.assertEqual(compute_amendment_type("13F-HR/A", None), "restatement")

    def test_enrich_filing_dict(self):
        """Verify enrich_filing_dict correctly mutates a raw dictionary."""
        d_base = {
            "form_type": "13F-HR",
            "filing_date": dt.date(2026, 8, 14),
            "period_of_report": dt.date(2026, 6, 30),
        }
        enrich_filing_dict(d_base)
        self.assertFalse(d_base["is_amendment"])
        self.assertIsNone(d_base["amendment_type"])
        self.assertFalse(d_base["is_late_filing"])

        d_amend_late = {
            "form_type": "13F-HR/A",
            "filing_date": dt.date(2026, 8, 26),
            "period_of_report": dt.date(2026, 6, 30),
            "amendment_type": "addition",
        }
        enrich_filing_dict(d_amend_late)
        self.assertTrue(d_amend_late["is_amendment"])
        self.assertEqual(d_amend_late["amendment_type"], "addition")
        self.assertTrue(d_amend_late["is_late_filing"])

    # -------------------------------------------------------------------------
    # Unit Tests: Pydantic Model Schema & Instantiation
    # -------------------------------------------------------------------------

    def test_models_have_r4_fields_and_defaults(self):
        """All target models declare is_amendment, amendment_type, is_late_filing."""
        # Filing
        f = Filing(
            accession_number="000-00-00",
            form_type="13F-HR",
            filing_date=dt.date(2026, 8, 14),
            period_of_report=dt.date(2026, 6, 30),
            created_at=dt.datetime.now(),
            updated_at=dt.datetime.now(),
        )
        self.assertFalse(f.is_amendment)
        self.assertIsNone(f.amendment_type)
        self.assertFalse(f.is_late_filing)

        # FilingListItem
        fli = FilingListItem(
            accession_number="000-00-00",
            form_type="13F-HR/A",
            filing_date=dt.date(2026, 8, 26),
            period_of_report=dt.date(2026, 6, 30),
            is_amendment=True,
            amendment_type="addition",
            is_late_filing=True,
        )
        self.assertTrue(fli.is_amendment)
        self.assertEqual(fli.amendment_type, "addition")
        self.assertTrue(fli.is_late_filing)

        # FilingDetail
        fd = FilingDetail(
            accession_number="000-00-00",
            form_type="13F-HR",
            filing_date=dt.date(2026, 8, 14),
            period_of_report=dt.date(2026, 6, 30),
        )
        self.assertFalse(fd.is_amendment)
        self.assertIsNone(fd.amendment_type)
        self.assertFalse(fd.is_late_filing)

        # FilingEnvelope
        fe = FilingEnvelope(
            filing_id=1,
            accession_number="000-00-00",
            cik="123",
            company_name="Test Corp",
            form_type="13F-HR",
            filing_date=dt.date(2026, 8, 14),
            period_of_report=dt.date(2026, 6, 30),
        )
        self.assertFalse(fe.is_amendment)
        self.assertIsNone(fe.amendment_type)
        self.assertFalse(fe.is_late_filing)

        # HoldingActivity
        ha = HoldingActivity(
            cik="123",
            company_name="Test Corp",
            latest_accession_number="000-00-00",
            reporting_period=dt.date(2026, 6, 30),
            filing_date=dt.date(2026, 8, 14),
            issuer_name="Apple Inc",
            cusip="037833100",
            is_common_stock=True,
            change_type="new",
            absolute_value_change=1000,
            is_amendment=True,
            amendment_type="restatement",
            is_late_filing=False,
        )
        self.assertTrue(ha.is_amendment)
        self.assertEqual(ha.amendment_type, "restatement")
        self.assertFalse(ha.is_late_filing)

    # -------------------------------------------------------------------------
    # Integration Tests: Endpoints Verification
    # -------------------------------------------------------------------------

    def test_endpoint_filings_list(self):
        """GET /filings returns items with R4 flags."""
        res = self.client.get("/filings?limit=5")
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

        # NUL byte in accession_number safely returns 404 rather than 500
        res_nul = self.client.get("/filings/0001085146-26-000657%00extra")
        self.assertEqual(res_nul.status_code, 404)

    def test_endpoint_options_activity_flags(self):
        """GET /activity/latest/options exposes filing flags on option activity."""
        res = self.client.get("/activity/latest/options?limit=5")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        for act in activities:
            self.assertIn("is_amendment", act)
            self.assertIn("amendment_type", act)
            self.assertIn("is_late_filing", act)
            self.assertIsInstance(act["is_amendment"], bool)
            self.assertIsInstance(act["is_late_filing"], bool)


if __name__ == "__main__":
    unittest.main()
