"""
tests/test_sync_endpoints.py

Tests for the Content Hub change-feed endpoints:
- GET /changes/head   (opaque head cursor)  — auth: service account + realm role
- GET /changes        (source-less feed)     — auth: service account + realm role
- GET /stream         (SSE doorbell)         — public

TestChangeCursor is a pure unit test (no DB, no auth). The endpoint tests are
integration tests requiring a populated PostgreSQL. The `/changes*` endpoints now
require a service-account bearer token, so their data assertions only run when
`TEST_SYNC_TOKEN` is set (the auth-enforcement checks always run).
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import change_feed
from change_feed import encode_cursor, decode_cursor
from main import app

# Service-account token for the protected sync endpoints. Unset in CI, so the
# data-bearing tests below skip there; the auth-enforcement tests still run.
SYNC_TOKEN = os.getenv("TEST_SYNC_TOKEN")
_auth = {"Authorization": f"Bearer {SYNC_TOKEN}"} if SYNC_TOKEN else {}
requires_token = unittest.skipUnless(
    SYNC_TOKEN, "set TEST_SYNC_TOKEN (service-account JWT) to run protected sync tests"
)


class TestChangeCursor(unittest.TestCase):
    """Opaque (updated_at, filing_id) cursor — no database or auth required."""

    def test_roundtrip(self):
        ts = datetime(2026, 9, 23, 14, 3, 22, 123456, tzinfo=timezone.utc)
        token = encode_cursor(ts, 288001)
        iso, filing_id = decode_cursor(token)
        self.assertEqual(filing_id, 288001)
        self.assertEqual(iso, "2026-09-23T14:03:22.123456Z")

    def test_opaque(self):
        token = encode_cursor(datetime(2026, 1, 1, tzinfo=timezone.utc), 42)
        self.assertNotEqual(token, "42")
        self.assertNotIn("_", token[:1])

    def test_ordering_preserved(self):
        a = decode_cursor(encode_cursor(datetime(2026, 1, 1, tzinfo=timezone.utc), 10))
        b = decode_cursor(encode_cursor(datetime(2026, 1, 2, tzinfo=timezone.utc), 1))
        self.assertLess((a[0], a[1]), (b[0], b[1]))

    def test_naive_datetime_treated_as_utc(self):
        naive = encode_cursor(datetime(2026, 9, 23, 14, 0, 0, 0), 5)
        aware = encode_cursor(datetime(2026, 9, 23, 14, 0, 0, 0, tzinfo=timezone.utc), 5)
        self.assertEqual(naive, aware)

    def test_invalid_tokens_raise(self):
        for bad in ["", "not-base64-!!", "not_a_number", encode_cursor(datetime.now(timezone.utc), 1)[:-3]]:
            with self.assertRaises(ValueError):
                decode_cursor(bad)


class _FakeDb:
    """Minimal psycopg2-cursor stand-in: serves one filing page, records SQL."""

    def __init__(self, page):
        self.page = page
        self.queries = []
        self._rows = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        self._rows = self.page if "FROM filings f" in sql else []

    def fetchall(self):
        return self._rows

    def mogrify(self, template, args):
        def lit(v):
            return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"
        return (template % tuple(lit(a) for a in args)).encode("utf-8")


class TestFetchChangesSinglePage(unittest.TestCase):
    """Regression: limit=1 on a first-ever filing (no predecessor) returned 500.

    A one-row VALUES list holding a bare NULL previous_filing_id is typed text by
    Postgres, so `filing_id = fwp.previous_filing_id` raised
    "operator does not exist: integer = text". The nullable predecessor columns
    must carry explicit casts so the type never depends on sibling rows.
    """

    def test_null_predecessor_is_cast_in_values(self):
        now = datetime(2026, 8, 17, 21, 16, 56, tzinfo=timezone.utc)
        page = [{
            "filing_id": 338561, "company_id": 1, "accession_number": "0000000000-26-000001",
            "period_of_report": "2026-06-30", "filing_date": "2026-08-17",
            "created_at": now, "updated_at": now, "form_type": "13F-HR",
            "file_number": None, "filing_directory": None,
            "company_name": "Acme", "cik_number": "1", "aum": None,
            "previous_filing_id": None, "previous_accession_number": None,
        }]
        db = _FakeDb(page)
        items, next_cursor, has_more = change_feed.fetch_changes(db, None, 1, 0, "backward")

        self.assertEqual(len(items), 1)
        self.assertTrue(has_more)
        activity_sql = db.queries[-1]
        self.assertIn("NULL::integer, NULL::text)", activity_sql)


class TestSyncAuth(unittest.TestCase):
    """Auth policy on the sync endpoints (runs without a token / DB)."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_changes_requires_auth(self):
        """/changes rejects an unauthenticated caller."""
        self.assertIn(self.client.get("/changes").status_code, (401, 403))

    def test_changes_head_requires_auth(self):
        self.assertIn(self.client.get("/changes/head").status_code, (401, 403))

    def test_stream_is_public(self):
        """GET /stream?once=true is public and delivers the connected greeting."""
        with self.client.stream("GET", "/stream?once=true") as stream_response:
            self.assertEqual(stream_response.status_code, 200)
            self.assertIn("text/event-stream", stream_response.headers.get("content-type", ""))
            lines = []
            for line in stream_response.iter_lines():
                if line:
                    lines.append(line)
                if len(lines) >= 2:
                    break
            text = "\n".join(lines)
            self.assertIn("event: connected", text)
            self.assertIn('"head":', text)


@requires_token
class TestSyncEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_changes_head(self):
        """GET /changes/head returns an opaque cursor that decodes."""
        response = self.client.get("/changes/head", headers=_auth)
        self.assertEqual(response.status_code, 200)
        iso, filing_id = decode_cursor(response.json()["head_cursor"])
        self.assertGreaterEqual(filing_id, 0)

    def test_changes_feed_shape(self):
        """GET /changes returns complete filings with embedded holdings."""
        response = self.client.get("/changes?limit=3", headers=_auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("items", data)
        self.assertIn("next_cursor", data)
        self.assertIn("has_more", data)
        self.assertLessEqual(len(data["items"]), 3)
        if not data["items"]:
            return
        env = data["items"][0]
        for field in (
            "filing_id", "accession_number", "cik", "company_name", "form_type",
            "filing_date", "period_of_report", "file_number", "filing_directory",
            "created_at", "updated_at", "activities",
        ):
            self.assertIn(field, env)
        self.assertIsNotNone(data["next_cursor"])
        decode_cursor(data["next_cursor"])
        if env["activities"]:
            act = env["activities"][0]
            for field in ("issuer_name", "cusip", "change_type", "is_common_stock",
                          "put_or_call", "weight_pct", "value_pct", "form_type"):
                self.assertIn(field, act)

    def test_changes_feed_cursor_walk(self):
        """A page's next_cursor advances the walk without overlap."""
        first = self.client.get("/changes?limit=2", headers=_auth).json()
        if not first["items"] or not first["next_cursor"]:
            return
        second = self.client.get(f"/changes?limit=2&cursor={first['next_cursor']}", headers=_auth)
        self.assertEqual(second.status_code, 200)
        first_ids = {i["filing_id"] for i in first["items"]}
        second_ids = {i["filing_id"] for i in second.json()["items"]}
        self.assertTrue(first_ids.isdisjoint(second_ids))

    def test_changes_backward_walk(self):
        """direction=backward returns newest-first (DESC by updated_at, filing_id)."""
        data = self.client.get("/changes?direction=backward&limit=3", headers=_auth).json()
        items = data["items"]
        if len(items) < 2:
            return
        keys = [(i["updated_at"], i["filing_id"]) for i in items]
        self.assertEqual(keys, sorted(keys, reverse=True))

    def test_changes_invalid_direction(self):
        """An unknown direction is a 422 (validation error)."""
        response = self.client.get("/changes?direction=sideways", headers=_auth)
        self.assertEqual(response.status_code, 422)

    def test_changes_feed_draining_to_null(self):
        """A cursor past the end yields empty items and a null next_cursor."""
        far_future = encode_cursor(datetime(2999, 1, 1, tzinfo=timezone.utc), 0)
        response = self.client.get(f"/changes?cursor={far_future}&limit=10", headers=_auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["items"], [])
        self.assertIsNone(data["next_cursor"])
        self.assertFalse(data["has_more"])

    def test_changes_invalid_cursor(self):
        """A malformed cursor is a 400."""
        response = self.client.get("/changes?cursor=not_a_valid_cursor", headers=_auth)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid cursor", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
