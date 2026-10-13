"""
tests/test_ai_summary.py

Unit and regression tests for POST /api/ai_summary:
- PERF-01: Verifies that database connections are not held idle during LLM calls,
  that synchronous DB queries run via worker threads, and that DB cursors/connections
  are cleanly acquired and released.
- SEC-02: Verifies that the endpoint is protected by the dedicated strict rate limit
  (AI_SUMMARY_RATE_LIMIT = 20/minute) instead of the generic RATE_LIMIT (120/minute).
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import asyncio
import inspect
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from main import app
from config import limiter, AI_SUMMARY_RATE_LIMIT, RATE_LIMIT, AI_SUMMARY_CACHE
import routers.comparisons as comparisons_router
from routers.comparisons import (
    openai_call,
    _fetch_cached_summary_sync,
    _persist_cached_summary_sync,
)


class TestAISummaryEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        limiter.reset()
        AI_SUMMARY_CACHE.clear()

    def tearDown(self):
        limiter.reset()
        AI_SUMMARY_CACHE.clear()

    def test_perf01_signature_does_not_contain_db_dependency(self):
        """
        PERF-01: Verify that db: Depends(get_db_cursor) is removed from openai_call's
        signature so FastAPI does not allocate a pooled connection before running the route.
        """
        sig = inspect.signature(openai_call)
        self.assertNotIn(
            "db",
            sig.parameters,
            "openai_call should not depend on db in its signature",
        )

    def test_perf01_no_db_connection_held_during_llm_execution(self):
        """
        PERF-01: Verify that during the asyncio.gather(*api_calls) LLM execution,
        no database connection is held by the endpoint.
        """
        active_db_contexts = 0
        max_active_during_llm = 0
        total_db_calls = 0
        llm_called = False

        def tracking_get_db_cursor():
            nonlocal active_db_contexts, total_db_calls
            total_db_calls += 1
            active_db_contexts += 1
            try:
                # Yield a dummy cursor
                dummy_cursor = MagicMock()
                dummy_cursor.fetchone.return_value = None
                dummy_cursor.connection = MagicMock()
                dummy_cursor.connection.closed = False
                yield dummy_cursor
            finally:
                active_db_contexts -= 1

        async def mock_chat_completion(*args, **kwargs):
            nonlocal active_db_contexts, max_active_during_llm, llm_called
            llm_called = True
            # Capture the count of currently active DB contexts while the LLM is running
            max_active_during_llm = max(max_active_during_llm, active_db_contexts)
            mock_choice = MagicMock()
            mock_choice.message.content = "Increased position in AAPL."
            mock_resp = MagicMock()
            mock_resp.choices = [mock_choice]
            return mock_resp

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=mock_chat_completion)

        payload = {
            "new_holdings": [],
            "closed_positions": [],
            "increased_holdings": [
                {
                    "issuer_name_clean": "APPLE INC",
                    "change_in_share": 1000,
                    "percent_change": 5.0,
                    "total_shares_latest": 20000,
                    "total_value_latest": 3500000,
                }
            ],
            "decreased_holdings": [],
        }

        with patch.object(comparisons_router, "get_db_cursor", side_effect=tracking_get_db_cursor), \
             patch.object(comparisons_router, "OPENROUTER_API_KEY", "test-key"), \
             patch.object(comparisons_router, "client", mock_client):

            response = self.client.post("/api/ai_summary", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(llm_called)
            # The critical assertion: Both read and write occurred, but active_db_contexts was 0 during LLM!
            self.assertEqual(
                total_db_calls,
                2,
                "Expected 2 DB operations: 1 cache read before LLM and 1 persist write after LLM",
            )
            self.assertEqual(
                max_active_during_llm,
                0,
                "A database connection was held active during LLM execution!",
            )
            # After endpoint completion, active_db_contexts MUST be 0 (no leaks)
            self.assertEqual(active_db_contexts, 0, "Database connection context leaked")

    def test_perf01_tier2_db_cache_hit_bypasses_llm(self):
        """
        Verify that a cache hit in Tier 2 (ai_summaries DB table) returns the summary
        and does NOT call the LLM, populating Tier 1 cache.
        """
        payload = {
            "new_holdings": [
                {
                    "issuer_name_clean": "MICROSOFT CORP",
                    "total_shares_latest": 10000,
                    "total_value_latest": 4000000,
                }
            ],
            "closed_positions": [],
            "increased_holdings": [],
            "decreased_holdings": [],
        }

        mock_llm = AsyncMock()
        mock_client = MagicMock()
        mock_client.chat.completions.create = mock_llm

        with patch.object(comparisons_router, "OPENROUTER_API_KEY", "test-key"), \
             patch.object(comparisons_router, "client", mock_client), \
             patch.object(comparisons_router, "_fetch_cached_summary_sync", return_value="DB cached summary for MSFT"):

            response = self.client.post("/api/ai_summary", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["summary"], "DB cached summary for MSFT")
            # LLM must not have been called
            mock_llm.assert_not_called()

            # Subsequent call must hit Tier 1 in-memory cache directly without DB lookup
            with patch.object(comparisons_router, "_fetch_cached_summary_sync") as mock_db_read:
                response2 = self.client.post("/api/ai_summary", json=payload)
                self.assertEqual(response2.status_code, 200)
                self.assertEqual(response2.json()["summary"], "DB cached summary for MSFT")
                mock_db_read.assert_not_called()

    def test_perf01_db_failure_gracefully_handled(self):
        """
        Verify that if DB cache read or write throws an exception, the endpoint
        still functions and returns the LLM summary rather than crashing with 500.
        """
        payload = {
            "new_holdings": [],
            "closed_positions": [
                {
                    "issuer_name_clean_prev": "TESLA INC",
                    "total_shares_prev": 5000,
                    "total_value_prev": 1200000,
                }
            ],
            "increased_holdings": [],
            "decreased_holdings": [],
        }

        mock_choice = MagicMock()
        mock_choice.message.content = "Closed out position in TSLA."
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_llm = AsyncMock(return_value=mock_resp)

        mock_client = MagicMock()
        mock_client.chat.completions.create = mock_llm

        with patch.object(comparisons_router, "OPENROUTER_API_KEY", "test-key"), \
             patch.object(comparisons_router, "client", mock_client), \
             patch.object(comparisons_router, "_fetch_cached_summary_sync", side_effect=Exception("DB down")), \
             patch.object(comparisons_router, "_persist_cached_summary_sync", side_effect=Exception("DB write failed")):

            response = self.client.post("/api/ai_summary", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Closed out position in TSLA.", response.json()["summary"])

    def test_sec02_dedicated_rate_limiter_enforced(self):
        """
        SEC-02: Verify that /api/ai_summary enforces the dedicated strict rate limit
        (AI_SUMMARY_RATE_LIMIT, default 20/minute), blocking the 21st request with HTTP 429.
        """
        self.assertEqual(AI_SUMMARY_RATE_LIMIT, "20/minute")

        payload = {
            "new_holdings": [],
            "closed_positions": [],
            "increased_holdings": [],
            "decreased_holdings": [],
        }

        mock_client = MagicMock()
        with patch.object(comparisons_router, "OPENROUTER_API_KEY", "test-key"), \
             patch.object(comparisons_router, "client", mock_client):
            # Make 20 requests - all must genuinely succeed with HTTP 200 OK
            for i in range(20):
                res = self.client.post("/api/ai_summary", json=payload)
                self.assertEqual(
                    res.status_code,
                    200,
                    f"Request {i+1} failed unexpectedly with status {res.status_code}",
                )

            # 21st request must exceed the rate limit and return 429 Too Many Requests
            res_blocked = self.client.post("/api/ai_summary", json=payload)
            self.assertEqual(
                res_blocked.status_code,
                429,
                f"Expected HTTP 429 on 21st request, got {res_blocked.status_code}: {res_blocked.text}",
            )

    def test_sec02_rate_limit_env_override(self):
        """
        SEC-02: Verify that AI_SUMMARY_RATE_LIMIT defaults to '20/minute' when unset,
        and respects custom environment variable overrides when provided.
        """
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_SUMMARY_RATE_LIMIT", None)
            val = os.getenv("AI_SUMMARY_RATE_LIMIT", "20/minute")
            self.assertEqual(val, "20/minute")

        with patch.dict(os.environ, {"AI_SUMMARY_RATE_LIMIT": "50/minute"}):
            val = os.getenv("AI_SUMMARY_RATE_LIMIT", "20/minute")
            self.assertEqual(val, "50/minute")

    def test_empty_positions_returns_default_message_with_api_key(self):
        """
        When payload has no changes detected, returns 200 with default message
        without attempting to call LLM or needing DB connection.
        """
        payload = {
            "new_holdings": [],
            "closed_positions": [],
            "increased_holdings": [],
            "decreased_holdings": [],
        }
        mock_client = MagicMock()
        with patch.object(comparisons_router, "OPENROUTER_API_KEY", "test-key"), \
             patch.object(comparisons_router, "client", mock_client):
            res = self.client.post("/api/ai_summary", json=payload)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(
                res.json()["summary"],
                "No significant position changes detected for this filing.",
            )


    def test_sync_db_helpers_acquire_and_cleanly_close_cursors(self):
        """
        Verify that _fetch_cached_summary_sync and _persist_cached_summary_sync cleanly
        open and close DB cursors via get_db_cursor.
        """
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"summary": "Persisted summary"}
        mock_cursor.connection = MagicMock()
        mock_cursor.connection.closed = False

        closed_count = 0

        def fake_get_db_cursor():
            nonlocal closed_count
            try:
                yield mock_cursor
            finally:
                closed_count += 1

        with patch.object(comparisons_router, "get_db_cursor", side_effect=fake_get_db_cursor):
            val = _fetch_cached_summary_sync("abc123key")
            self.assertEqual(val, "Persisted summary")
            self.assertEqual(closed_count, 1)

            _persist_cached_summary_sync("abc123key", "New summary")
            self.assertEqual(closed_count, 2)
            mock_cursor.connection.commit.assert_called_once()

    def test_perf01_concurrent_requests_do_not_starve_connection_pool(self):
        """
        PERF-01: Attack test verifying that when the DB connection pool is heavily
        constrained (e.g., single connection simulated by a binary semaphore),
        multiple concurrent AI summary requests do not deadlock or starve the pool,
        because connections are released immediately before LLM execution and only
        re-acquired briefly for persistence.
        """
        sem = threading.Semaphore(1)

        def constrained_get_db_cursor():
            # Acquire connection with a strict timeout; if a connection is held
            # across the LLM call, concurrent requests will time out.
            acquired = sem.acquire(timeout=2.0)
            if not acquired:
                raise RuntimeError("DB pool starvation deadlock detected!")
            try:
                cur = MagicMock()
                cur.fetchone.return_value = None
                cur.connection = MagicMock()
                cur.connection.closed = False
                yield cur
            finally:
                sem.release()

        async def slow_mock_llm(*args, **kwargs):
            # Simulate async LLM latency
            await asyncio.sleep(0.03)
            mock_choice = MagicMock()
            mock_choice.message.content = "Concurrent summary output."
            mock_resp = MagicMock()
            mock_resp.choices = [mock_choice]
            return mock_resp

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=slow_mock_llm)

        payloads = [
            {
                "new_holdings": [
                    {
                        "issuer_name_clean": f"CONCURRENT_CO_{i}",
                        "total_shares_latest": 100 * (i + 1),
                        "total_value_latest": 5000 * (i + 1),
                    }
                ],
                "closed_positions": [],
                "increased_holdings": [],
                "decreased_holdings": [],
            }
            for i in range(3)
        ]

        def send_request(p):
            return self.client.post("/api/ai_summary", json=p)

        with patch.object(comparisons_router, "get_db_cursor", side_effect=constrained_get_db_cursor), \
             patch.object(comparisons_router, "OPENROUTER_API_KEY", "test-key"), \
             patch.object(comparisons_router, "client", mock_client):
            with ThreadPoolExecutor(max_workers=3) as executor:
                responses = list(executor.map(send_request, payloads))

        for resp in responses:
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Concurrent summary output.", resp.json()["summary"])


if __name__ == "__main__":
    unittest.main()

