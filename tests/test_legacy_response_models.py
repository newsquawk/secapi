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
)


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

    def test_get_filings_response_model(self):
        r = self.client.get("/filings?limit=2")
        self.assertEqual(r.status_code, 200)
        res = FilingsListResponse(**r.json())
        self.assertEqual(len(res.filings), 2)
        self.assertEqual(res.sorting.current_sort_by, "filing_date")

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

    def test_search_filings_by_aum_response_model(self):
        r = self.client.get("/api/v1/search/filings_by_aum?limit=2")
        self.assertEqual(r.status_code, 200)
        res = FilingsByAumResponse(**r.json())
        self.assertEqual(len(res.filings), 2)


if __name__ == "__main__":
    unittest.main()
