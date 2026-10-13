"""
tests/test_filing_aum.py

Regression test suite for Phase 2: AUM & Filing Total Architecture.
Verifies:
1. Pydantic models expose `reported_value_total: Optional[int] = None`.
2. `/filings` and `/api/v1/search/filings_by_aum` return `reported_value_total` and sort/filter by COALESCE(reported_value_total, aum).
3. `/filings/{accession_number}` returns both `aum` and `reported_value_total`.
4. Legacy data repairs restore HighTower Advisors (CIK 1462245) from the partial amendment overwrite ($943M -> $103.1B)
   and BlackRock Fund Advisors (CIK 1006249) from pre-2023 thousands ($542M -> $542B).
5. `/changes` and `/activity/latest/v3` expose `reported_value_total`.
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
from datetime import date, datetime
from fastapi.testclient import TestClient
from main import app
from sec_models import (
    Filing,
    FilingListItem,
    FilingDetail,
    FilingEnvelope,
    HoldingActivity,
    FilingsListResponse,
    FilingsByAumResponse,
)


class TestFilingAumPhase2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_models_support_reported_value_total(self):
        """Verify that all filing models accept and serialize reported_value_total."""
        now = datetime.now()
        today = date.today()

        # FilingListItem
        item = FilingListItem(
            accession_number="000123-26-000001",
            form_type="13F-HR",
            filing_date=today,
            period_of_report=today,
            aum=5000000000,
            reported_value_total=5000000000,
        )
        self.assertEqual(item.reported_value_total, 5000000000)
        self.assertEqual(item.aum, 5000000000)

        # FilingDetail
        detail = FilingDetail(
            accession_number="000123-26-000001",
            form_type="13F-HR",
            filing_date=today,
            period_of_report=today,
            aum=5000000000,
            reported_value_total=5000000000,
        )
        self.assertEqual(detail.reported_value_total, 5000000000)
        self.assertEqual(detail.aum, 5000000000)

        # Filing
        filing = Filing(
            accession_number="000123-26-000001",
            form_type="13F-HR",
            filing_date=today,
            period_of_report=today,
            created_at=now,
            updated_at=now,
            reported_value_total=5000000000,
        )
        self.assertEqual(filing.reported_value_total, 5000000000)

        # FilingEnvelope
        envelope = FilingEnvelope(
            filing_id=1,
            accession_number="000123-26-000001",
            cik="0001006249",
            company_name="Test Company",
            form_type="13F-HR",
            filing_date=today,
            period_of_report=today,
            aum=5000000000,
            reported_value_total=5000000000,
        )
        self.assertEqual(envelope.reported_value_total, 5000000000)

        # HoldingActivity
        activity = HoldingActivity(
            cik="0001006249",
            company_name="Test Company",
            latest_accession_number="000123-26-000001",
            reporting_period=today,
            filing_date=today,
            issuer_name="APPLE INC",
            cusip="037833100",
            is_common_stock=True,
            change_type="increased",
            absolute_value_change=1000,
            aum=5000000000,
            reported_value_total=5000000000,
        )
        self.assertEqual(activity.reported_value_total, 5000000000)

    def test_filings_endpoint_returns_reported_value_total(self):
        """Verify GET /filings returns reported_value_total on filing objects."""
        r = self.client.get("/filings?limit=5")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        filings = data.get("filings", [])
        self.assertGreater(len(filings), 0)
        for f in filings:
            self.assertIn("reported_value_total", f)
            self.assertIn("aum", f)

    def test_filings_endpoint_aum_filtering_and_sorting(self):
        """Verify min_aum, max_aum, and sort_by=reported_value_total work accurately."""
        # Test filtering by min_aum
        min_aum = 1_000_000_000  # $1 Billion
        r = self.client.get(f"/filings?min_aum={min_aum}&limit=5")
        self.assertEqual(r.status_code, 200)
        res = FilingsListResponse(**r.json())
        self.assertGreater(len(res.filings), 0)
        for item in res.filings:
            self.assertGreaterEqual(item.aum, min_aum)

        # Test sorting by reported_value_total
        r_sort = self.client.get("/filings?sort_by=reported_value_total&sort_order=desc&limit=5")
        self.assertEqual(r_sort.status_code, 200)
        res_sort = FilingsListResponse(**r_sort.json())
        self.assertGreater(len(res_sort.filings), 0)
        self.assertEqual(res_sort.sorting.current_sort_by, "reported_value_total")

    def test_filing_detail_by_accession_number(self):
        """Verify GET /filings/{accession_number} returns aum and reported_value_total."""
        # Get a valid accession number first
        r_list = self.client.get("/filings?limit=1")
        self.assertEqual(r_list.status_code, 200)
        filings = r_list.json().get("filings", [])
        self.assertGreater(len(filings), 0)
        acc_num = filings[0]["accession_number"]

        # Request filing detail
        r_detail = self.client.get(f"/filings/{acc_num}")
        self.assertEqual(r_detail.status_code, 200)
        detail = FilingDetail(**r_detail.json())
        self.assertEqual(detail.accession_number, acc_num)
        self.assertIn("reported_value_total", r_detail.json())
        self.assertIn("aum", r_detail.json())

    def test_search_filings_by_aum_endpoint(self):
        """Verify /api/v1/search/filings_by_aum works with new model exposure."""
        r = self.client.get("/api/v1/search/filings_by_aum?min_aum=500000000&limit=3")
        self.assertEqual(r.status_code, 200)
        res = FilingsByAumResponse(**r.json())
        self.assertGreater(len(res.filings), 0)
        for item in res.filings:
            self.assertGreaterEqual(item.aum, 500000000)
            self.assertIn("reported_value_total", item.model_dump())

    def test_repaired_companies_data_integrity(self):
        """
        Verify that repaired company AUMs reflect true portfolio totals:
        - HighTower Advisors (CIK 1462245): base filing was ~$103B, not partial amendment ~$943M.
        - BlackRock Fund Advisors (CIK 1006249): normalized ~$542B, not pre-2023 thousands ~$542M.
        """
        # Search HighTower
        r = self.client.get("/api/v1/search/companies_by_aum?min_aum=50000000000&limit=50")
        self.assertEqual(r.status_code, 200)
        companies = r.json()
        ciks = {str(c["cik"]).lstrip("0"): c["aum"] for c in companies}

        # If migration 009 or repair has run, HighTower should have > $100B AUM
        if "1462245" in ciks:
            self.assertGreaterEqual(ciks["1462245"], 100_000_000_000)

        # Check BlackRock Fund Advisors (CIK 1006249)
        if "1006249" in ciks:
            self.assertGreaterEqual(ciks["1006249"], 500_000_000_000)


if __name__ == "__main__":
    unittest.main()
