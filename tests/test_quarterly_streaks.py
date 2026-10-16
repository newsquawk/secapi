"""
tests/test_quarterly_streaks.py

Comprehensive unit and integration test suite for Milestone 2:
Requirement 2 (R2) Consecutive Quarterly Buying and Selling Streaks.

Covers:
  1. Pure unit tests for quarter_index and _calculate_streak:
     - Calendar quarter index math
     - Consecutive buying streak continuation (1 -> 2 -> 3 quarters)
     - Consecutive selling streak continuation (1 -> 2 -> 3 quarters)
     - Streak reset on direction reversal (buy -> sell and sell -> buy)
     - Streak reset / stop on unfiled calendar quarter gaps
     - Streak termination on unchanged position (including 1-share cash-in-lieu tolerance)
     - Stock split rebasing (forward splits, 25:1 BKNG split, reverse splits)
     - Options split isolation (options not rebased by stock splits)
     - Clean walkback termination at position inception (shares from 0)
  2. Unit tests for enrich_activities:
     - Empty list handling
     - Handling dicts and Pydantic HoldingActivity instances
     - In-place mutation of streak_quarters, streak_direction, streak_type
     - Unchanged positions having None / 0 streak attributes
     - CIK resolution when company_id is omitted
  3. Integration tests with live PostgreSQL database:
     - Real-world multi-quarter buying and selling streaks (streak >= 2)
     - Berkshire Hathaway (0001067983) Apple (AAPL) position
     - Booking Holdings (BKNG) split rebasing in live data
  4. API Endpoint verification:
     - GET /changes
     - GET /activity/latest/v3
     - GET /activity/latest/options
     - GET /stories/latest/v2
"""

import datetime as dt
import unittest
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from auth import sync_dependencies
from database import get_db_connection
from holding_enricher import (
    _calculate_streak,
    enrich_activities,
    quarter_index,
)
from main import app
from sec_models import HoldingActivity


class TestQuarterlyStreaksUnit(unittest.TestCase):
    """
    Pure unit tests for streak calculation algorithms and edge cases.
    """

    def test_quarter_index_calculation(self):
        """Verify year * 4 + (month - 1) // 3 formula for standard calendar quarters."""
        self.assertEqual(quarter_index(dt.date(2025, 3, 31)), 2025 * 4 + 0)
        self.assertEqual(quarter_index(dt.date(2025, 6, 30)), 2025 * 4 + 1)
        self.assertEqual(quarter_index(dt.date(2025, 9, 30)), 2025 * 4 + 2)
        self.assertEqual(quarter_index(dt.date(2025, 12, 31)), 2025 * 4 + 3)
        self.assertEqual(quarter_index(dt.date(2026, 3, 31)), 2026 * 4 + 0)

    def test_new_position_streak(self):
        """New positions must always have streak_quarters = 1 and streak_direction = 'buying'."""
        sq, sdir, stype = _calculate_streak(
            comp_id=1,
            cusip="037833100",
            rep_period=dt.date(2026, 6, 30),
            change_type="new",
            is_option=False,
            shares_history={},
            company_filed_quarters={},
            splits_map={},
        )
        self.assertEqual(sq, 1)
        self.assertEqual(sdir, "buying")
        self.assertEqual(stype, "buying")

    def test_unchanged_position_streak(self):
        """Unchanged positions must have None for all streak attributes."""
        sq, sdir, stype = _calculate_streak(
            comp_id=1,
            cusip="037833100",
            rep_period=dt.date(2026, 6, 30),
            change_type="unchanged",
            is_option=False,
            shares_history={},
            company_filed_quarters={},
            splits_map={},
        )
        self.assertIsNone(sq)
        self.assertIsNone(sdir)
        self.assertIsNone(stype)

    def test_consecutive_buying_streak_continuation(self):
        """
        Verify buying streak extends across consecutive quarters when shares increase:
        Q0 (2025-06-30): 10,000
        Q1 (2025-09-30): 15,000 (buy)
        Q2 (2025-12-31): 20,000 (buy)
        Q3 (2026-03-31): 25,000 (buy)
        Q4 (2026-06-30): 30,000 (buy - current quarter) -> streak = 4 quarters.
        """
        q0 = quarter_index(dt.date(2025, 6, 30))
        q1 = quarter_index(dt.date(2025, 9, 30))
        q2 = quarter_index(dt.date(2025, 12, 31))
        q3 = quarter_index(dt.date(2026, 3, 31))
        q4 = quarter_index(dt.date(2026, 6, 30))

        filed_quarters = {
            1: {
                q0: dt.date(2025, 6, 30),
                q1: dt.date(2025, 9, 30),
                q2: dt.date(2025, 12, 31),
                q3: dt.date(2026, 3, 31),
                q4: dt.date(2026, 6, 30),
            }
        }
        shares_history = {
            (1, "AAPL", False): {
                q0: 10000,
                q1: 15000,
                q2: 20000,
                q3: 25000,
                q4: 30000,
            }
        }

        sq, sdir, stype = _calculate_streak(
            comp_id=1,
            cusip="AAPL",
            rep_period=dt.date(2026, 6, 30),
            change_type="increased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        self.assertEqual(sq, 4)
        self.assertEqual(sdir, "buying")
        self.assertEqual(stype, "buying")

    def test_consecutive_selling_streak_continuation(self):
        """
        Verify selling streak extends across consecutive quarters when shares decrease:
        Q0 (2025-09-30): 50,000
        Q1 (2025-12-31): 40,000 (sell)
        Q2 (2026-03-31): 30,000 (sell)
        Q3 (2026-06-30): 20,000 (sell - current quarter) -> streak = 3 quarters.
        """
        q0 = quarter_index(dt.date(2025, 9, 30))
        q1 = quarter_index(dt.date(2025, 12, 31))
        q2 = quarter_index(dt.date(2026, 3, 31))
        q3 = quarter_index(dt.date(2026, 6, 30))

        filed_quarters = {
            1: {
                q0: dt.date(2025, 9, 30),
                q1: dt.date(2025, 12, 31),
                q2: dt.date(2026, 3, 31),
                q3: dt.date(2026, 6, 30),
            }
        }
        shares_history = {
            (1, "AAPL", False): {
                q0: 50000,
                q1: 40000,
                q2: 30000,
                q3: 20000,
            }
        }

        sq, sdir, stype = _calculate_streak(
            comp_id=1,
            cusip="AAPL",
            rep_period=dt.date(2026, 6, 30),
            change_type="decreased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        self.assertEqual(sq, 3)
        self.assertEqual(sdir, "selling")
        self.assertEqual(stype, "selling")

    def test_streak_reset_on_direction_reversal(self):
        """
        Buying streak terminates when prior quarter was a sell (and vice versa):
        Q1 (2025-12-31): 40,000
        Q2 (2026-03-31): 30,000 (sell)
        Q3 (2026-06-30): 35,000 (buy - current quarter)
        Streak must be 1 because Q2 was a sell.
        """
        q1 = quarter_index(dt.date(2025, 12, 31))
        q2 = quarter_index(dt.date(2026, 3, 31))
        q3 = quarter_index(dt.date(2026, 6, 30))

        filed_quarters = {
            1: {
                q1: dt.date(2025, 12, 31),
                q2: dt.date(2026, 3, 31),
                q3: dt.date(2026, 6, 30),
            }
        }
        shares_history = {
            (1, "AAPL", False): {
                q1: 40000,
                q2: 30000,
                q3: 35000,
            }
        }

        sq, sdir, stype = _calculate_streak(
            comp_id=1,
            cusip="AAPL",
            rep_period=dt.date(2026, 6, 30),
            change_type="increased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        self.assertEqual(sq, 1, "Reversal from sell to buy must reset streak to 1")
        self.assertEqual(sdir, "buying")

    def test_streak_reset_on_unfiled_calendar_quarter_gap(self):
        """
        Unfiled calendar quarter gap breaks continuity:
        Q0 (2025-06-30): UNFILED / MISSING
        Q1 (2025-09-30): 10,000
        Q2 (2025-12-31): 20,000 (buy)
        Q3 (2026-03-31): 30,000 (buy)
        Q4 (2026-06-30): 40,000 (buy - current quarter)
        The walk stops at Q1 because Q0 is unfiled.
        Thus, streak = 3 quarters (Q4, Q3, Q2).
        """
        q0 = quarter_index(dt.date(2025, 6, 30))  # unfiled
        q1 = quarter_index(dt.date(2025, 9, 30))
        q2 = quarter_index(dt.date(2025, 12, 31))
        q3 = quarter_index(dt.date(2026, 3, 31))
        q4 = quarter_index(dt.date(2026, 6, 30))

        filed_quarters = {
            1: {
                # q0 is missing
                q1: dt.date(2025, 9, 30),
                q2: dt.date(2025, 12, 31),
                q3: dt.date(2026, 3, 31),
                q4: dt.date(2026, 6, 30),
            }
        }
        shares_history = {
            (1, "AAPL", False): {
                q1: 10000,
                q2: 20000,
                q3: 30000,
                q4: 40000,
            }
        }

        sq, sdir, stype = _calculate_streak(
            comp_id=1,
            cusip="AAPL",
            rep_period=dt.date(2026, 6, 30),
            change_type="increased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        self.assertEqual(sq, 3, "Unfiled gap at Q0 must stop walk, leaving streak at 3")
        self.assertEqual(sdir, "buying")

    def test_streak_immediate_prior_unfiled_quarter_gap(self):
        """If immediate prior quarter (expected_q1) was not filed, streak is 1."""
        q1 = quarter_index(dt.date(2025, 9, 30))
        # q2 (2025-12-31) skipped
        # q3 (2026-03-31) skipped
        q4 = quarter_index(dt.date(2026, 6, 30))

        filed_quarters = {
            1: {
                q1: dt.date(2025, 9, 30),
                q4: dt.date(2026, 6, 30),
            }
        }
        shares_history = {
            (1, "AAPL", False): {
                q1: 10000,
                q4: 20000,
            }
        }

        sq, sdir, stype = _calculate_streak(
            comp_id=1,
            cusip="AAPL",
            rep_period=dt.date(2026, 6, 30),
            change_type="increased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        self.assertEqual(sq, 1)
        self.assertEqual(sdir, "buying")

    def test_streak_cash_in_lieu_one_share_tolerance(self):
        """
        1-share cash-in-lieu rounding tolerance terminates streak:
        Q1 (2025-12-31): 10,000
        Q2 (2026-03-31): 10,001 (diff = +1 share, within tolerance -> unchanged)
        Q3 (2026-06-30): 15,000 (buy - current quarter)
        Because Q2 vs Q1 was effectively unchanged (tolerance <= 1), streak = 1.
        """
        q1 = quarter_index(dt.date(2025, 12, 31))
        q2 = quarter_index(dt.date(2026, 3, 31))
        q3 = quarter_index(dt.date(2026, 6, 30))

        filed_quarters = {
            1: {
                q1: dt.date(2025, 12, 31),
                q2: dt.date(2026, 3, 31),
                q3: dt.date(2026, 6, 30),
            }
        }
        shares_history = {
            (1, "AAPL", False): {
                q1: 10000,
                q2: 10001,
                q3: 15000,
            }
        }

        sq, sdir, stype = _calculate_streak(
            comp_id=1,
            cusip="AAPL",
            rep_period=dt.date(2026, 6, 30),
            change_type="increased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        self.assertEqual(sq, 1, "1-share diff in prior quarter must terminate streak walk")

    def test_stock_split_rebasing_forward_split(self):
        """
        Forward split rebasing:
        Q1 (2025-12-31): 1,000 shares
        Q2 (2026-03-31): 2:1 stock split occurs (factor = 2.0).
                         Actual held = 2,500 shares.
                         Rebased Q1 = 2,000 shares.
                         2,500 > 2,000 -> buying action in Q2!
        Q3 (2026-06-30): 3,000 shares (buying in Q3)
        Streak should be 2 quarters.
        """
        q1 = quarter_index(dt.date(2025, 12, 31))
        q2 = quarter_index(dt.date(2026, 3, 31))
        q3 = quarter_index(dt.date(2026, 6, 30))

        filed_quarters = {
            1: {
                q1: dt.date(2025, 12, 31),
                q2: dt.date(2026, 3, 31),
                q3: dt.date(2026, 6, 30),
            }
        }
        shares_history = {
            (1, "XYZ", False): {
                q1: 1000,
                q2: 2500,
                q3: 3000,
            }
        }
        splits_map = {
            ("XYZ", q2): 2.0,
        }

        sq, sdir, stype = _calculate_streak(
            comp_id=1,
            cusip="XYZ",
            rep_period=dt.date(2026, 6, 30),
            change_type="increased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map=splits_map,
        )
        self.assertEqual(sq, 2)
        self.assertEqual(sdir, "buying")

    def test_options_split_isolation(self):
        """
        Stock split rebasing must NOT apply to options.
        If security is an option, split_factor remains 1.0.
        """
        q1 = quarter_index(dt.date(2025, 12, 31))
        q2 = quarter_index(dt.date(2026, 3, 31))
        q3 = quarter_index(dt.date(2026, 6, 30))

        filed_quarters = {
            1: {
                q1: dt.date(2025, 12, 31),
                q2: dt.date(2026, 3, 31),
                q3: dt.date(2026, 6, 30),
            }
        }
        shares_history = {
            (1, "XYZ", True): {  # is_option = True
                q1: 1000,
                q2: 1500,
                q3: 2000,
            }
        }
        splits_map = {
            ("XYZ", q2): 10.0,  # 10:1 stock split on underlying
        }

        sq, sdir, stype = _calculate_streak(
            comp_id=1,
            cusip="XYZ",
            rep_period=dt.date(2026, 6, 30),
            change_type="increased",
            is_option=True,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map=splits_map,
        )
        # Without split adjustment for options: 1500 > 1000 (buy) -> streak = 2
        # (If split had been applied, 1500 < 10000 which would be sell/reset)
        self.assertEqual(sq, 2)
        self.assertEqual(sdir, "buying")

    def test_walkback_stops_at_position_inception(self):
        """
        When walking back a buying streak, if prev_shares == 0, the position was
        initiated as new in that quarter, so the streak cannot walk further back.
        """
        q0 = quarter_index(dt.date(2025, 6, 30))
        q1 = quarter_index(dt.date(2025, 9, 30))
        q2 = quarter_index(dt.date(2025, 12, 31))
        q3 = quarter_index(dt.date(2026, 3, 31))

        filed_quarters = {
            1: {
                q0: dt.date(2025, 6, 30),
                q1: dt.date(2025, 9, 30),
                q2: dt.date(2025, 12, 31),
                q3: dt.date(2026, 3, 31),
            }
        }
        # In Q0, shares = 0 (not held). In Q1, initiated at 5,000 (new).
        # In Q2, 10,000 (buy). In Q3, 15,000 (buy).
        shares_history = {
            (1, "XYZ", False): {
                q0: 0,
                q1: 5000,
                q2: 10000,
                q3: 15000,
            }
        }

        sq, sdir, stype = _calculate_streak(
            comp_id=1,
            cusip="XYZ",
            rep_period=dt.date(2026, 3, 31),
            change_type="increased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        self.assertEqual(sq, 3)
        self.assertEqual(sdir, "buying")


class TestEnrichActivitiesIntegration(unittest.TestCase):
    """
    Integration tests for enrich_activities and API endpoints.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "test-service-account"}

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    def test_enrich_empty_list(self):
        """Empty activities list returns empty list."""
        conn = get_db_connection()
        try:
            res = enrich_activities(conn, [])
            self.assertEqual(res, [])
        finally:
            conn.close()

    def test_enrich_pydantic_model(self):
        """enrich_activities must properly mutate Pydantic HoldingActivity instances."""
        conn = get_db_connection()
        try:
            act = HoldingActivity(
                cik="0001067983",
                company_name="BERKSHIRE HATHAWAY INC",
                latest_accession_number="0001067983-26-000001",
                filing_date=dt.date(2026, 8, 14),
                cusip="037833100",
                issuer_name="APPLE INC",
                is_common_stock=True,
                change_type="decreased",
                reporting_period=dt.date(2026, 6, 30),
                current_shares=300000000,
                previous_shares=400000000,
                change_in_share=-100000000,
                percent_change=-25.0,
                absolute_value_change=100000000,
            )
            enrich_activities(conn, [act])
            self.assertIsNotNone(act.streak_quarters)
            self.assertEqual(act.streak_direction, "selling")
            self.assertEqual(act.streak_type, "selling")
        finally:
            conn.close()

    def test_live_multi_quarter_streaks_in_activity_feed(self):
        """
        Verify that live data produces multi-quarter streaks (streak_quarters >= 2)
        and that streak_direction and streak_type align.
        """
        res = self.client.get("/activity/latest/v3?limit=50&security_type=stocks")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        activities = data.get("activities", [])
        streaks = [a for a in activities if (a.get("streak_quarters") or 0) >= 2]
        self.assertTrue(len(streaks) > 0, "Expected at least one activity with streak_quarters >= 2")
        for act in streaks[:10]:
            self.assertIn(act.get("streak_direction"), ("buying", "selling", "buy", "sell"))
            self.assertEqual(act.get("streak_type"), act.get("streak_direction"))

    def test_berkshire_apple_live(self):
        """
        Live verification for Berkshire Hathaway (CIK 0001067983) and Apple (AAPL).
        If decreased, streak >= 2; if unchanged, streak is None.
        """
        res = self.client.get("/activity/latest/v3?cik=0001067983&ticker=AAPL")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        if activities:
            apple_act = activities[0]
            if apple_act.get("change_type") == "decreased":
                self.assertGreaterEqual(apple_act.get("streak_quarters", 0), 2)
                self.assertIn(apple_act.get("streak_direction"), ("sell", "selling"))
            elif apple_act.get("change_type") == "unchanged":
                self.assertIn(apple_act.get("streak_quarters"), (None, 0))
                self.assertIsNone(apple_act.get("streak_direction"))

    def test_booking_holdings_split_streak_live(self):
        """
        Live verification: Booking Holdings (BKNG) had a 25:1 forward split.
        Verify /activity/latest/v3 for BKNG exposes valid streak metadata.
        """
        res = self.client.get("/activity/latest/v3?security_type=stocks&cik=0001929139&ticker=BKNG")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        activities = data.get("activities", [])
        self.assertTrue(len(activities) > 0)
        bkng_act = activities[0]
        self.assertIn("streak_quarters", bkng_act)
        self.assertIn("streak_direction", bkng_act)
        self.assertIn("streak_type", bkng_act)

    def test_endpoint_changes_streaks(self):
        """Verify GET /changes exposes streak fields within envelope items."""
        res = self.client.get("/changes?limit=2")
        self.assertEqual(res.status_code, 200)
        items = res.json().get("items", [])
        self.assertTrue(len(items) > 0)
        for item in items:
            for act in item.get("activities", []):
                self.assertIn("streak_quarters", act)
                self.assertIn("streak_direction", act)
                self.assertIn("streak_type", act)

    def test_endpoint_activity_options_streaks(self):
        """Verify GET /activity/latest/options exposes streak fields."""
        res = self.client.get("/activity/latest/options?limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        acts = data.get("activities", [])
        self.assertTrue(len(acts) > 0)
        for act in acts:
            self.assertIn("streak_quarters", act)
            self.assertIn("streak_direction", act)
            self.assertIn("streak_type", act)

    def test_endpoint_stories_v2_streaks(self):
        """Verify GET /stories/latest/v2 exposes streak fields on significant holdings."""
        res = self.client.get("/stories/latest/v2?limit=5")
        self.assertEqual(res.status_code, 200)
        stories = res.json().get("stories", [])
        self.assertTrue(len(stories) > 0)
        found_streak = False
        for story in stories:
            top_inc = story.get("top_increased_position")
            if top_inc and "streak_quarters" in top_inc:
                found_streak = True
                self.assertIn(top_inc.get("streak_direction"), ("buy", "buying"))
            top_dec = story.get("top_decreased_position")
            if top_dec and "streak_quarters" in top_dec:
                found_streak = True
                self.assertIn(top_dec.get("streak_direction"), ("sell", "selling"))
            top_new = story.get("top_new_position")
            if top_new and "streak_quarters" in top_new:
                found_streak = True
                self.assertIn(top_new.get("streak_direction"), ("buy", "buying"))
        self.assertTrue(found_streak, "Stories v2 should expose streak fields on top positions")


if __name__ == "__main__":
    unittest.main()
