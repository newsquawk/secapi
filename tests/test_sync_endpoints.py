"""
tests/test_sync_endpoints.py

Integration tests for the Content Hub 13F Sync Architecture endpoints:
- GET /changes/head
- GET /changes/{source}
- GET /stream
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
from fastapi.testclient import TestClient
from main import app


class TestSyncEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_changes_head(self):
        """Verify GET /changes/head returns a valid numeric head cursor."""
        response = self.client.get("/changes/head")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("head_cursor", data)
        head_int = int(data["head_cursor"])
        self.assertGreater(head_int, 0)

    def test_changes_feed_stocks(self):
        """Verify GET /changes/stocks returns filing envelopes with enriched activities[]."""
        response = self.client.get("/changes/stocks?cursor=288000&limit=3")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertIn("items", data)
        self.assertIn("next_cursor", data)
        self.assertIn("has_more", data)
        self.assertEqual(len(data["items"]), 3)
        self.assertTrue(data["has_more"])

        # Check first envelope
        first_filing = data["items"][0]
        self.assertIn("filing_id", first_filing)
        self.assertIn("accession_number", first_filing)
        self.assertIn("cik", first_filing)
        self.assertIn("company_name", first_filing)
        self.assertIn("form_type", first_filing)
        self.assertIn("activities", first_filing)

        # Check next_cursor matches the 3rd filing_id
        third_filing_id = str(data["items"][2]["filing_id"])
        self.assertEqual(data["next_cursor"], third_filing_id)

        # Check activity enrichment fields
        if first_filing["activities"]:
            first_act = first_filing["activities"][0]
            self.assertIn("issuer_name", first_act)
            self.assertIn("cusip", first_act)
            self.assertIn("change_type", first_act)
            self.assertIn("weight_pct", first_act)
            self.assertIn("value_pct", first_act)
            self.assertIn("form_type", first_act)

    def test_changes_feed_options(self):
        """Verify GET /changes/options returns options contract activities."""
        # Filing 30431 is known to contain option positions
        response = self.client.get("/changes/options?cursor=30430&limit=2")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("items", data)
        self.assertGreater(len(data["items"]), 0)

        # Find the envelope for 30431
        target_envelope = next((item for item in data["items"] if item["filing_id"] == 30431), None)
        self.assertIsNotNone(target_envelope)
        self.assertGreater(len(target_envelope["activities"]), 0)
        first_act = target_envelope["activities"][0]
        self.assertIn(first_act["put_or_call"].upper(), ("PUT", "CALL"))

    def test_changes_feed_draining_to_null(self):
        """Verify that when no more filings exist beyond cursor, next_cursor is null."""
        response = self.client.get("/changes/stocks?cursor=999999999&limit=10")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["items"], [])
        self.assertIsNone(data["next_cursor"])
        self.assertFalse(data["has_more"])

    def test_changes_invalid_source(self):
        """Verify 400 Bad Request for unrecognized source."""
        response = self.client.get("/changes/invalid_source_xyz")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown source", response.json()["detail"])

    def test_changes_invalid_cursor(self):
        """Verify 400 Bad Request for non-numeric cursor."""
        response = self.client.get("/changes/stocks?cursor=not_a_number")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid cursor", response.json()["detail"])

    def test_stream_initial_connection(self):
        """Verify GET /stream connects as an SSE stream and delivers the initial greeting event."""
        with self.client.stream("GET", "/stream?once=true") as stream_response:
            self.assertEqual(stream_response.status_code, 200)
            self.assertIn("text/event-stream", stream_response.headers.get("content-type", ""))
            # Read the first event frame
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
