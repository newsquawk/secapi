"""
tests/test_legacy_response_models.py

Validates that legacy manager and filing endpoints strictly conform to
their newly defined Pydantic response models.
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
from fastapi.testclient import TestClient
from main import app
from sec_models import (
    ManagerSummary,
    ManagerFilingsResponse,
    CompanySearchResult,
    CompanyAumRank,
    FilingsListResponse,
    FilingsByAumResponse,
    ManagersListResponse,
    CompaniesByAumResponse,
    LatestActivityResponse,
)
from routers.activity import LatestStoriesResponse


class TestLegacyResponseModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_get_managers_response_model(self):
        r = self.client.get("/managers?limit=2")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsInstance(data, list)
        if data:
            item = ManagerSummary(**data[0])
            self.assertTrue(item.cik)
        self.assertIn("x-total-count", r.headers)
        self.assertIn("x-has-more", r.headers)
        self.assertIn("x-next-offset", r.headers)
        self.assertEqual(r.headers.get("x-next-offset"), "2")

    def test_get_managers_enveloped(self):
        r = self.client.get("/managers?limit=2&envelope=true")
        self.assertEqual(r.status_code, 200)
        res = ManagersListResponse(**r.json())
        self.assertIsInstance(res.managers, list)
        self.assertEqual(res.pagination.limit, 2)
        self.assertEqual(res.pagination.offset, 0)
        self.assertTrue(res.pagination.has_more)
        self.assertEqual(res.pagination.next_offset, 2)
        self.assertGreater(res.pagination.total, 0)

    def test_get_manager_detail_response_model(self):
        # 0001067983 (Berkshire Hathaway)
        r = self.client.get("/managers/0001067983")
        self.assertEqual(r.status_code, 200)
        item = ManagerSummary(**r.json())
        self.assertIn("BERKSHIRE", item.company_name.upper())

    def test_get_manager_filings_response_model(self):
        r = self.client.get("/managers/0001067983/filings?limit=2")
        self.assertEqual(r.status_code, 200)
        res = ManagerFilingsResponse(**r.json())
        self.assertGreater(len(res.filings), 0)
        self.assertIn("x-total-count", r.headers)
        self.assertIn("x-has-more", r.headers)
        self.assertIsNotNone(res.pagination.next_offset)

    def test_get_filings_response_model(self):
        r = self.client.get("/filings?limit=2")
        self.assertEqual(r.status_code, 200)
        res = FilingsListResponse(**r.json())
        self.assertEqual(len(res.filings), 2)
        self.assertEqual(res.sorting.current_sort_by, "filing_date")
        self.assertIn("x-total-count", r.headers)
        self.assertIn("x-has-more", r.headers)
        self.assertEqual(res.pagination.next_offset, 2)

    def test_search_companies_response_model(self):
        r = self.client.get("/api/search/companies?q=Apple")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsInstance(data, list)
        if data:
            item = CompanySearchResult(**data[0])
            self.assertTrue(item.name)

    def test_search_companies_by_aum_response_model(self):
        r = self.client.get("/api/v1/search/companies_by_aum?limit=2")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsInstance(data, list)
        if data:
            item = CompanyAumRank(**data[0])
            self.assertTrue(item.cik)
        self.assertIn("x-total-count", r.headers)
        self.assertIn("x-has-more", r.headers)
        self.assertEqual(r.headers.get("x-next-offset"), "2")

    def test_search_companies_by_aum_enveloped(self):
        r = self.client.get("/api/v1/search/companies_by_aum?limit=2&envelope=true")
        self.assertEqual(r.status_code, 200)
        res = CompaniesByAumResponse(**r.json())
        self.assertIsInstance(res.companies, list)
        self.assertEqual(res.pagination.limit, 2)
        self.assertEqual(res.pagination.offset, 0)
        self.assertTrue(res.pagination.has_more)
        self.assertEqual(res.pagination.next_offset, 2)
        self.assertGreater(res.pagination.total, 0)

    def test_search_filings_by_aum_response_model(self):
        r = self.client.get("/api/v1/search/filings_by_aum?limit=2")
        self.assertEqual(r.status_code, 200)
        res = FilingsByAumResponse(**r.json())
        self.assertEqual(len(res.filings), 2)
        self.assertIn("x-total-count", r.headers)
        self.assertIn("x-has-more", r.headers)
        self.assertEqual(res.pagination.next_offset, 2)

    def test_activity_v3_pagination(self):
        r = self.client.get("/activity/latest/v3?limit=2")
        self.assertEqual(r.status_code, 200)
        res = LatestActivityResponse(**r.json())
        self.assertIn("x-has-more", r.headers)
        self.assertIsNotNone(res.pagination)
        self.assertEqual(res.pagination.limit, 2)
        self.assertEqual(res.pagination.offset, 0)

    def test_options_activity_pagination(self):
        r = self.client.get("/activity/latest/options?limit=2")
        self.assertEqual(r.status_code, 200)
        res = LatestActivityResponse(**r.json())
        self.assertIn("x-has-more", r.headers)
        self.assertIsNotNone(res.pagination)
        self.assertEqual(res.pagination.limit, 2)
        self.assertEqual(res.pagination.offset, 0)

    def test_stories_v2_pagination(self):
        r = self.client.get("/stories/latest/v2?limit=2")
        self.assertEqual(r.status_code, 200)
        res = LatestStoriesResponse(**r.json())
        self.assertIn("x-has-more", r.headers)
        self.assertIsNotNone(res.pagination)
        self.assertEqual(res.pagination.limit, 2)
        self.assertEqual(res.pagination.offset, 0)

    def test_filings_aum_filtering(self):
        r = self.client.get("/filings?min_aum=100000000&limit=5")
        self.assertEqual(r.status_code, 200)
        res = FilingsListResponse(**r.json())
        self.assertIsInstance(res.filings, list)
        self.assertGreater(len(res.filings), 0)
        for filing in res.filings:
            self.assertGreaterEqual(filing.aum, 100000000)

    def test_managers_aum_filtering(self):
        r = self.client.get("/managers?min_aum=100000000&limit=5")
        self.assertEqual(r.status_code, 200)
        items = [ManagerSummary(**item) for item in r.json()]
        self.assertGreater(len(items), 0)
        for item in items:
            self.assertTrue(item.cik)
            self.assertTrue(item.company_name)

    def test_activity_v3_options_dispatch(self):
        r = self.client.get("/activity/latest/v3?security_type=options&limit=2")
        self.assertEqual(r.status_code, 200)
        res = LatestActivityResponse(**r.json())
        self.assertIsNotNone(res.pagination)
        self.assertEqual(res.pagination.limit, 2)

    def test_activity_v3_invalid_security_type(self):
        r = self.client.get("/activity/latest/v3?security_type=cryptocurrency")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Invalid security_type", r.json().get("detail", ""))


if __name__ == "__main__":
    unittest.main()

