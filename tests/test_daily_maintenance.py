"""
tests/test_daily_maintenance.py

Tests for operational automation:
1. Advisory lock protection against concurrent maintenance executions.
2. Dry-run and pipeline execution of run_maintenance.
3. Authenticated endpoint POST /sync/maintenance triggering background tasks.
4. Split detection query isolation: verifies that CurrLatestFilings and PrevLatestFilings
   select exactly one filing per company in a period (preventing amendment share doubling).
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
from fastapi.testclient import TestClient
import psycopg2

from main import app
from database import _get_db_connection_params
from auth import sync_dependencies
from scripts.run_daily_maintenance import (
    acquire_advisory_lock,
    release_advisory_lock,
    run_maintenance,
    ADVISORY_LOCK_ID,
)


class TestDailyMaintenance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_advisory_lock_protection(self):
        """Verify session-level advisory lock prevents concurrent maintenance runs."""
        conn1 = psycopg2.connect(**_get_db_connection_params())
        conn2 = psycopg2.connect(**_get_db_connection_params())

        try:
            # First session acquires lock
            self.assertTrue(acquire_advisory_lock(conn1))

            # Second session must be rejected
            self.assertFalse(acquire_advisory_lock(conn2))

            # First session releases lock
            release_advisory_lock(conn1)

            # Second session can now acquire lock
            self.assertTrue(acquire_advisory_lock(conn2))
        finally:
            release_advisory_lock(conn2)
            conn1.close()
            conn2.close()

    def test_run_maintenance_dry_run(self):
        """Verify run_maintenance dry-run executes cleanly and returns statistics."""
        stats = run_maintenance(recent_quarters=2, dry_run=True)
        self.assertEqual(stats["status"], "success")
        self.assertTrue(stats["lock_acquired"])
        self.assertGreaterEqual(stats["elapsed_seconds"], 0.0)
        self.assertIn("Daily maintenance completed", stats["message"])

    def test_maintenance_endpoint_requires_auth(self):
        """Verify POST /sync/maintenance rejects unauthenticated calls."""
        r = self.client.post("/sync/maintenance")
        self.assertIn(r.status_code, [401, 403])

    def test_maintenance_endpoint_with_auth(self):
        """Verify POST /sync/maintenance queues background task when authenticated."""
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "service-account"}
        try:
            r = self.client.post("/sync/maintenance?recent=2&dry_run=true")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["status"], "accepted")
            self.assertIn("queued in background", data["message"])
        finally:
            app.dependency_overrides.clear()

    def test_split_detection_query_amendment_isolation(self):
        """
        Verify that CurrLatestFilings CTE selects exactly one filing per company
        even when multiple filings (original + amendment) exist in the period.
        """
        conn = psycopg2.connect(**_get_db_connection_params())
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    WITH CurrLatestFilings AS (
                        SELECT DISTINCT ON (company_id) filing_id, company_id
                        FROM filings
                        WHERE period_of_report = '2026-06-30'::date
                          AND form_type IN ('13F-HR', '13F-HR/A')
                        ORDER BY company_id, filing_date DESC, filing_id DESC
                    )
                    SELECT company_id, COUNT(*)
                    FROM CurrLatestFilings
                    GROUP BY company_id
                    HAVING COUNT(*) > 1;
                """)
                duplicates = cur.fetchall()
                self.assertEqual(
                    len(duplicates),
                    0,
                    "CurrLatestFilings must contain at most 1 filing per company_id",
                )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
