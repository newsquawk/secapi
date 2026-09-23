"""
tests/test_sync_endpoints.py

Tests for the Content Hub change-feed endpoints:
- GET /changes/head   (opaque head cursor)
- GET /changes        (source-less feed of complete filings)
- GET /stream         (SSE doorbell)

TestChangeCursor is a pure unit test (no DB). TestSyncEndpoints is an
integration test and requires a populated PostgreSQL (as the originals did).
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


class TestChangeCursor(unittest.TestCase):
    """Opaque (updated_at, filing_id) cursor — no database required."""

    def test_roundtrip(self):
        ts = datetime(2026, 9, 23, 14, 3, 22, 123456, tzinfo=timezone.utc)
        token = encode_cursor(ts, 288001)
        iso, filing_id = decode_cursor(token)
        self.assertEqual(filing_id, 288001)
        self.assertEqual(iso, "2026-09-23T14:03:22.123456Z")

    def test_opaque(self):
        """The cursor must not be a bare, arithmetic-able filing id."""
        token = encode_cursor(datetime(2026, 1, 1, tzinfo=timezone.utc), 42)
        self.assertNotEqual(token, "42")
        self.assertNotIn("_", token[:1])  # base64url, not the raw "<iso>_<id>"

    def test_ordering_preserved(self):
        """Lexical order of the decoded (ts, id) matches chronological order."""
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


class TestSyncEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_changes_head(self):
        """GET /changes/head returns an opaque cursor that decodes."""
        response = self.client.get("/changes/head")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("head_cursor", data)
        iso, filing_id = decode_cursor(data["head_cursor"])  # must not raise
        self.assertGreaterEqual(filing_id, 0)

    def test_changes_feed_shape(self):
        """GET /changes returns complete filings with embedded holdings."""
        response = self.client.get("/changes?limit=3")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertIn("items", data)
        self.assertIn("next_cursor", data)
        self.assertIn("has_more", data)
        self.assertLessEqual(len(data["items"]), 3)

        if not data["items"]:
            return

        env = data["items"][0]
        # Filing-level fields, incl. the four added for the Filings widget.
        for field in (
            "filing_id", "accession_number", "cik", "company_name", "form_type",
            "filing_date", "period_of_report", "file_number", "filing_directory",
            "created_at", "updated_at", "activities",
        ):
            self.assertIn(field, env)

        # next_cursor is opaque and resumable on a non-empty page.
        self.assertIsNotNone(data["next_cursor"])
        decode_cursor(data["next_cursor"])  # must not raise

        # Holdings carry both the common-stock tag and the option tag.
        acts = env["activities"]
        if acts:
            act = acts[0]
            for field in ("issuer_name", "cusip", "change_type", "is_common_stock",
                          "put_or_call", "weight_pct", "value_pct", "form_type"):
                self.assertIn(field, act)

    def test_changes_feed_cursor_walk(self):
        """A page's next_cursor advances the walk without error."""
        first = self.client.get("/changes?limit=2").json()
        if not first["items"] or not first["next_cursor"]:
            return
        second = self.client.get(f"/changes?limit=2&cursor={first['next_cursor']}")
        self.assertEqual(second.status_code, 200)
        # No overlap: the second page starts strictly after the first cursor.
        first_ids = {i["filing_id"] for i in first["items"]}
        second_ids = {i["filing_id"] for i in second.json()["items"]}
        self.assertTrue(first_ids.isdisjoint(second_ids))

    def test_changes_feed_draining_to_null(self):
        """A cursor past the end yields empty items and a null next_cursor."""
        far_future = encode_cursor(datetime(2999, 1, 1, tzinfo=timezone.utc), 0)
        response = self.client.get(f"/changes?cursor={far_future}&limit=10")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["items"], [])
        self.assertIsNone(data["next_cursor"])
        self.assertFalse(data["has_more"])

    def test_changes_invalid_cursor(self):
        """A malformed cursor is a 400."""
        response = self.client.get("/changes?cursor=not_a_valid_cursor")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid cursor", response.json()["detail"])

    def test_stream_initial_connection(self):
        """GET /stream?once=true delivers the connected greeting with a head."""
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


if __name__ == "__main__":
    unittest.main()
