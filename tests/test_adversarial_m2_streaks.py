"""
tests/test_adversarial_m2_streaks.py

Adversarial Fuzzing and Invariant Stress-Testing Suite for Milestone 2:
Requirement 2 (R2) Consecutive Quarterly Buying and Selling Streaks.
Authored by Empirical Challenger (challenger_m2_2).

This suite empirically validates:
1. Invariant: streak_direction == streak_type on 100% of responses across all endpoints and models.
2. Invariant: For change_type == 'new', streak_quarters == 1 and streak_direction in ('buying', 'buy').
3. Invariant: For change_type in ('increased', 'decreased', 'closed'), streak_quarters >= 1.
4. Invariant: For change_type == 'unchanged', streak_quarters is None or 0, and streak_direction is None.
5. Options contracts: verify options contracts remain unadjusted by stock split factors (forward & reverse).
6. Continuity reset: verify that an unfiled calendar quarter resets streak count to 1 (both unit oracle and live database).
7. Adversarial input fuzzing: malformed dates, whitespace, strange change_types, SQL injection strings, Pydantic objects.
8. Cash-in-lieu 1-share tolerance boundary testing: delta = 0, delta = 1, delta = 2.
9. Walkback depth and inception termination invariants.
10. Fallback path invariant preservation under simulated database failures.
"""

import datetime as dt
import os
import unittest
from typing import Any, Dict, List
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import psycopg2

from auth import sync_dependencies
from database import get_db_connection
from holding_enricher import (
    _calculate_streak,
    enrich_activities,
    quarter_index,
)
from main import app
from sec_models import HoldingActivity


class TestAdversarialM2Streaks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        for dep in sync_dependencies:
            app.dependency_overrides[dep.dependency] = lambda: {"sub": "test-service-account"}
        cls.conn = get_db_connection()

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        if cls.conn and not cls.conn.closed:
            cls.conn.close()

    # =========================================================================
    # 1. INVARIANT 1: streak_direction == streak_type ON ALL RESPONSES
    # =========================================================================

    def test_invariant_1_streak_direction_equals_streak_type_activity_v3(self):
        """
        Invariant 1: streak_direction == streak_type across /activity/latest/v3
        for stocks across multiple pages.
        """
        for offset in [0, 50]:
            res = self.client.get(f"/activity/latest/v3?limit=50&offset={offset}&security_type=stocks")
            self.assertEqual(res.status_code, 200)
            activities = res.json().get("activities", [])
            self.assertGreater(len(activities), 0, "Expected activities from /activity/latest/v3")
            for act in activities:
                self.assertIn("streak_direction", act)
                self.assertIn("streak_type", act)
                self.assertEqual(
                    act["streak_direction"],
                    act["streak_type"],
                    f"Invariant violated in /activity/latest/v3: streak_direction={act['streak_direction']} != streak_type={act['streak_type']}",
                )

    def test_invariant_1_streak_direction_equals_streak_type_options(self):
        """
        Invariant 1: streak_direction == streak_type across /activity/latest/options.
        """
        res = self.client.get("/activity/latest/options?limit=30")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        self.assertGreater(len(activities), 0, "Expected activities from /activity/latest/options")
        for act in activities:
            self.assertIn("streak_direction", act)
            self.assertIn("streak_type", act)
            self.assertEqual(
                act["streak_direction"],
                act["streak_type"],
                f"Invariant violated in /activity/latest/options: streak_direction={act['streak_direction']} != streak_type={act['streak_type']}",
            )

    def test_invariant_1_streak_direction_equals_streak_type_changes_feed(self):
        """
        Invariant 1: streak_direction == streak_type across /changes feed items.
        """
        res = self.client.get("/changes?limit=2")
        self.assertEqual(res.status_code, 200)
        items = res.json().get("items", [])
        self.assertGreater(len(items), 0, "Expected envelopes from /changes")
        checked_count = 0
        for env in items:
            for act in env.get("activities", []):
                self.assertIn("streak_direction", act)
                self.assertIn("streak_type", act)
                self.assertEqual(
                    act["streak_direction"],
                    act["streak_type"],
                    f"Invariant violated in /changes: streak_direction={act['streak_direction']} != streak_type={act['streak_type']}",
                )
                checked_count += 1
        self.assertGreater(checked_count, 0, "Expected at least one activity verified in /changes")

    def test_invariant_1_streak_direction_equals_streak_type_stories_v2(self):
        """
        Invariant 1: streak_direction == streak_type across /stories/latest/v2 top holdings.
        """
        res = self.client.get("/stories/latest/v2?limit=10")
        self.assertEqual(res.status_code, 200)
        stories = res.json().get("stories", [])
        self.assertGreater(len(stories), 0, "Expected stories from /stories/latest/v2")
        story_keys = [
            "top_new_position",
            "top_closed_position",
            "top_increased_position",
            "top_decreased_position",
            "top_new_other_securities",
            "top_closed_other_securities",
            "top_increased_other_securities",
            "top_decreased_other_securities",
        ]
        checked_story_items = 0
        for s in stories:
            for key in story_keys:
                item = s.get(key)
                if item:
                    self.assertIn("streak_direction", item)
                    self.assertIn("streak_type", item)
                    self.assertEqual(
                        item["streak_direction"],
                        item["streak_type"],
                        f"Invariant violated in /stories/latest/v2 ({key}): streak_direction={item['streak_direction']} != streak_type={item['streak_type']}",
                    )
                    checked_story_items += 1
        self.assertGreater(checked_story_items, 0, "Expected at least one story position verified")

    # =========================================================================
    # 2. INVARIANT 2: change_type == 'new' => streak_quarters == 1 & streak_direction in ('buying', 'buy')
    # =========================================================================

    def test_invariant_2_new_positions_have_streak_1_buying_endpoints(self):
        """
        Invariant 2: For change_type == 'new', streak_quarters must be exactly 1
        and streak_direction in ('buying', 'buy').
        """
        res = self.client.get("/activity/latest/v3?limit=50&security_type=stocks")
        self.assertEqual(res.status_code, 200)
        new_activities = [a for a in res.json().get("activities", []) if a.get("change_type") == "new"]
        self.assertGreater(len(new_activities), 0, "Expected new activities")
        for act in new_activities:
            self.assertEqual(
                act.get("streak_quarters"),
                1,
                f"Invariant 2 violated: new position has streak_quarters={act.get('streak_quarters')} != 1",
            )
            self.assertIn(
                act.get("streak_direction"),
                ("buying", "buy"),
                f"Invariant 2 violated: new position has streak_direction={act.get('streak_direction')}",
            )
            self.assertEqual(act.get("streak_direction"), act.get("streak_type"))

    def test_invariant_2_new_positions_with_prior_ownership_reentry(self):
        """
        Invariant 2: Even if a position is a 're_entry' (manager previously owned the stock years ago),
        the current action in this quarter is 'new', so streak_quarters MUST BE 1 and direction 'buying'.
        """
        # Berkshire Hathaway CUSIP 23331A109 (D R Horton) on 2026-06-30 is a verified re-entry
        items = [
            {
                "cik": "0001067983",
                "cusip": "23331A109",
                "period_of_report": dt.date(2026, 6, 30),
                "change_type": "new",
            }
        ]
        enrich_activities(self.conn, items)
        self.assertEqual(items[0]["position_entry_type"], "re_entry")
        self.assertEqual(items[0]["streak_quarters"], 1)
        self.assertEqual(items[0]["streak_direction"], "buying")
        self.assertEqual(items[0]["streak_type"], "buying")

    # =========================================================================
    # 3. INVARIANT 3: change_type in ('increased', 'decreased', 'closed') => streak_quarters >= 1
    # =========================================================================

    def test_invariant_3_active_changes_streak_greater_equal_1(self):
        """
        Invariant 3: For change_type in ('increased', 'decreased', 'closed'),
        streak_quarters must be >= 1, and direction must be 'buying' (increased) or 'selling' (decreased/closed).
        """
        res = self.client.get("/activity/latest/v3?limit=50&security_type=stocks")
        self.assertEqual(res.status_code, 200)
        activities = res.json().get("activities", [])
        for act in activities:
            ct = act.get("change_type")
            if ct in ("increased", "decreased", "closed"):
                sq = act.get("streak_quarters")
                self.assertIsNotNone(sq, f"streak_quarters must not be None for {ct}")
                self.assertGreaterEqual(
                    sq,
                    1,
                    f"Invariant 3 violated: streak_quarters={sq} < 1 for change_type={ct}",
                )
                if ct == "increased":
                    self.assertIn(act.get("streak_direction"), ("buying", "buy"))
                else:
                    self.assertIn(act.get("streak_direction"), ("selling", "sell"))

    # =========================================================================
    # 4. INVARIANT 4: change_type == 'unchanged' => streak_quarters in (None, 0) & streak_direction is None
    # =========================================================================

    def test_invariant_4_unchanged_positions_streak_none_or_zero(self):
        """
        Invariant 4: For change_type == 'unchanged', streak_quarters is None or 0,
        and streak_direction is None.
        """
        # Test unit calculation directly
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
        self.assertIn(sq, (None, 0))
        self.assertIsNone(sdir)
        self.assertIsNone(stype)

        # Test enrich_activities directly
        items = [
            {
                "cik": "0001067983",
                "cusip": "037833100",
                "period_of_report": dt.date(2026, 6, 30),
                "change_type": "unchanged",
            }
        ]
        enrich_activities(self.conn, items)
        self.assertIn(items[0]["streak_quarters"], (None, 0))
        self.assertIsNone(items[0]["streak_direction"])
        self.assertIsNone(items[0]["streak_type"])

    # =========================================================================
    # 5. OPTIONS CONTRACTS: MUST REMAIN UNADJUSTED BY STOCK SPLIT FACTORS
    # =========================================================================

    def test_options_unadjusted_by_forward_splits(self):
        """
        Options contracts: Under a 2:1, 4:1, or 25:1 forward stock split,
        option contract counts MUST NOT be multiplied by the split factor.

        Scenario:
          Underlying stock has a 2:1 forward split at Q1 2026 (split_factor = 2.0).
          Option contracts:
            Q1 2026: 150 contracts
            Q4 2025: 100 contracts
          Options perspective: 150 > 100 => Buying continuation (streak 2).
          If incorrectly treated as stock: 150 vs round(100 * 2.0) = 200 => 150 < 200 (Selling),
          which would terminate the buying streak at 1.
        """
        comp_id = 99999
        cusip = "037833100"
        q_curr = dt.date(2026, 6, 30)
        qi_q2 = quarter_index(q_curr)          # 2026 Q2
        qi_q1 = quarter_index(dt.date(2026, 3, 31)) # 2026 Q1
        qi_q0 = quarter_index(dt.date(2025, 12, 31))# 2025 Q4

        filed_quarters = {
            comp_id: {
                qi_q2: q_curr,
                qi_q1: dt.date(2026, 3, 31),
                qi_q0: dt.date(2025, 12, 31),
            }
        }
        # 2:1 stock split on underlying common stock at Q1
        splits_map = {(cusip, qi_q1): 2.0}

        # Option contracts history (is_option=True)
        shares_history_opt = {
            (comp_id, cusip, True): {
                qi_q1: 150,
                qi_q0: 100,
            }
        }

        sq_opt, sdir_opt, stype_opt = _calculate_streak(
            comp_id=comp_id,
            cusip=cusip,
            rep_period=q_curr,
            change_type="increased",
            is_option=True,
            shares_history=shares_history_opt,
            company_filed_quarters=filed_quarters,
            splits_map=splits_map,
        )

        self.assertEqual(sq_opt, 2, "Options contracts must not be split-adjusted; streak should be 2")
        self.assertEqual(sdir_opt, "buying")

        # Compare with Common stock history (is_option=False)
        shares_history_stock = {
            (comp_id, cusip, False): {
                qi_q1: 150,
                qi_q0: 100,
            }
        }
        sq_stock, sdir_stock, _ = _calculate_streak(
            comp_id=comp_id,
            cusip=cusip,
            rep_period=q_curr,
            change_type="increased",
            is_option=False,
            shares_history=shares_history_stock,
            company_filed_quarters=filed_quarters,
            splits_map=splits_map,
        )
        self.assertEqual(sq_stock, 1, "Stock with 2:1 split should be rebased to 200, breaking buying streak to 1")

    def test_options_unadjusted_by_reverse_splits(self):
        """
        Options contracts: Under a 1:2 or 1:5 reverse stock split,
        option contract counts MUST NOT be multiplied by fractional split factor.

        Scenario:
          Underlying stock has 1:2 reverse split at Q1 2026 (factor = 0.5).
          Option contracts:
            Q1 2026: 80 contracts
            Q4 2025: 100 contracts
          Options perspective: 80 < 100 => Selling continuation (streak 2).
          If incorrectly treated as stock: 80 vs round(100 * 0.5) = 50 => 80 > 50 (Buying),
          which would break the selling streak.
        """
        comp_id = 99998
        cusip = "999999999"
        q_curr = dt.date(2026, 6, 30)
        qi_q2 = quarter_index(q_curr)
        qi_q1 = quarter_index(dt.date(2026, 3, 31))
        qi_q0 = quarter_index(dt.date(2025, 12, 31))

        filed_quarters = {
            comp_id: {
                qi_q2: q_curr,
                qi_q1: dt.date(2026, 3, 31),
                qi_q0: dt.date(2025, 12, 31),
            }
        }
        splits_map = {(cusip, qi_q1): 0.5}

        # Option contracts history (is_option=True)
        shares_history_opt = {
            (comp_id, cusip, True): {
                qi_q1: 80,
                qi_q0: 100,
            }
        }

        sq_opt, sdir_opt, _ = _calculate_streak(
            comp_id=comp_id,
            cusip=cusip,
            rep_period=q_curr,
            change_type="decreased",
            is_option=True,
            shares_history=shares_history_opt,
            company_filed_quarters=filed_quarters,
            splits_map=splits_map,
        )
        self.assertEqual(sq_opt, 2, "Options contracts must remain unadjusted by reverse splits; streak should be 2")
        self.assertEqual(sdir_opt, "selling")

    def test_options_invariance_in_enrich_activities(self):
        """
        Verify that enrich_activities correctly flags options via put_or_call or security_type
        and isolates them from stock splits in live pipeline.
        """
        items = [
            {
                "cik": "0001067983",
                "cusip": "037833100",
                "period_of_report": dt.date(2026, 6, 30),
                "change_type": "increased",
                "put_or_call": "CALL",
                "security_type": "options",
            },
            {
                "cik": "0001067983",
                "cusip": "037833100",
                "period_of_report": dt.date(2026, 6, 30),
                "change_type": "decreased",
                "put_or_call": "PUT",
                "security_type": "options",
            },
        ]
        enrich_activities(self.conn, items)
        for act in items:
            self.assertIsNotNone(act["streak_quarters"])
            self.assertGreaterEqual(act["streak_quarters"], 1)
            self.assertEqual(act["streak_direction"], act["streak_type"])

    # =========================================================================
    # 6. CONTINUITY RESET: UNFILED CALENDAR QUARTER MUST RESET STREAK TO 1
    # =========================================================================

    def test_continuity_reset_immediate_preceding_quarter_unfiled(self):
        """
        Test continuity reset: If the immediate preceding calendar quarter (Q - 1)
        was NOT filed by the company, the streak count MUST reset to 1.

        Timeline:
          Current: Q3 2026 (filed)
          Preceding: Q2 2026 (UNFILED - skipped!)
          Prior: Q1 2026 (filed, 10,000 shares)
          Prior: Q4 2025 (filed, 5,000 shares)
        Even if shares were increasing in earlier filed quarters, the skipped Q2 breaks continuity!
        """
        comp_id = 88881
        cusip = "037833100"
        rep_period = dt.date(2026, 9, 30) # Q3 2026
        qi_q3 = quarter_index(rep_period)
        qi_q1 = quarter_index(dt.date(2026, 3, 31)) # Q1 2026 (Q2 is missing!)
        qi_q0 = quarter_index(dt.date(2025, 12, 31))

        # Filed quarters contains Q3, Q1, Q0 (Q2 omitted!)
        filed_quarters = {
            comp_id: {
                qi_q3: rep_period,
                qi_q1: dt.date(2026, 3, 31),
                qi_q0: dt.date(2025, 12, 31),
            }
        }
        shares_history = {
            (comp_id, cusip, False): {
                qi_q1: 10000,
                qi_q0: 5000,
            }
        }

        sq, sdir, stype = _calculate_streak(
            comp_id=comp_id,
            cusip=cusip,
            rep_period=rep_period,
            change_type="increased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        self.assertEqual(
            sq,
            1,
            f"Expected streak=1 due to unfiled Q2 2026 gap, but got {sq}",
        )
        self.assertEqual(sdir, "buying")
        self.assertEqual(stype, "buying")

    def test_continuity_reset_two_quarters_back_unfiled(self):
        """
        Test continuity reset: If Q - 1 was filed, but Q - 2 was unfiled,
        walkback must terminate at Q - 1 and cannot bridge the missing quarter.

        Timeline:
          Current: Q3 2026 (increased) -> base streak 1
          Q2 2026: filed (increased vs Q1? Q1 is UNFILED!)
          Q0 2025: filed
        Result: Walkback terminates at Q2, resulting in streak = 1 (or capped before the gap).
        """
        comp_id = 88882
        cusip = "037833100"
        rep_period = dt.date(2026, 9, 30) # Q3
        qi_q3 = quarter_index(rep_period)
        qi_q2 = quarter_index(dt.date(2026, 6, 30))
        # Q1 (qi_q2 - 1) is NOT filed!
        qi_q0 = quarter_index(dt.date(2025, 12, 31))

        filed_quarters = {
            comp_id: {
                qi_q3: rep_period,
                qi_q2: dt.date(2026, 6, 30),
                qi_q0: dt.date(2025, 12, 31),
            }
        }
        shares_history = {
            (comp_id, cusip, False): {
                qi_q2: 10000,
                qi_q0: 5000,
            }
        }

        sq, sdir, stype = _calculate_streak(
            comp_id=comp_id,
            cusip=cusip,
            rep_period=rep_period,
            change_type="increased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        self.assertEqual(
            sq,
            1,
            f"Expected streak=1 because Q1 was unfiled, terminating walkback, but got {sq}",
        )

    def test_continuity_reset_live_database_verification(self):
        """
        Live database test: SS&H Financial Advisors, Inc. (CIK 0001911735, company_id 3639)
        filed for 2026-06-30 and 2025-12-31, but SKIPPED 2026-03-31.
        Empirical check: Any activity on 2026-06-30 MUST have streak_quarters == 1.
        """
        items = [
            {
                "cik": "0001911735",
                "cusip": "002824100",  # Abbott Laboratories
                "period_of_report": dt.date(2026, 6, 30),
                "change_type": "decreased",
            },
            {
                "cik": "0001911735",
                "cusip": "023135106",  # Amazon
                "period_of_report": dt.date(2026, 6, 30),
                "change_type": "increased",
            },
        ]
        enrich_activities(self.conn, items)
        for act in items:
            self.assertEqual(
                act["streak_quarters"],
                1,
                f"CIK 0001911735 skipped 2026-03-31; streak should be 1, got {act['streak_quarters']}",
            )
            self.assertEqual(act["streak_direction"], act["streak_type"])

    # =========================================================================
    # 7. CASH-IN-LIEU 1-SHARE TOLERANCE BOUNDARY TESTING
    # =========================================================================

    def test_cash_in_lieu_boundary_delta_zero(self):
        """Delta = 0: Shares are identical; streak walkback must stop immediately."""
        comp_id = 77771
        cusip = "037833100"
        rep_period = dt.date(2026, 6, 30)
        qi_q2 = quarter_index(rep_period)
        qi_q1 = quarter_index(dt.date(2026, 3, 31))
        qi_q0 = quarter_index(dt.date(2025, 12, 31))

        filed_quarters = {
            comp_id: {qi_q2: rep_period, qi_q1: dt.date(2026, 3, 31), qi_q0: dt.date(2025, 12, 31)}
        }
        # Shares in Q1 == shares in Q0 => unchanged
        shares_history = {
            (comp_id, cusip, False): {
                qi_q1: 1000,
                qi_q0: 1000,
            }
        }
        sq, _, _ = _calculate_streak(
            comp_id=comp_id,
            cusip=cusip,
            rep_period=rep_period,
            change_type="increased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        self.assertEqual(sq, 1, "Delta 0 is unchanged, walkback must stop at 1")

    def test_cash_in_lieu_boundary_delta_one(self):
        """Delta = 1 (1 share liquidated as cash in lieu): Treated as unchanged; streak walkback stops."""
        comp_id = 77772
        cusip = "037833100"
        rep_period = dt.date(2026, 6, 30)
        qi_q2 = quarter_index(rep_period)
        qi_q1 = quarter_index(dt.date(2026, 3, 31))
        qi_q0 = quarter_index(dt.date(2025, 12, 31))

        filed_quarters = {
            comp_id: {qi_q2: rep_period, qi_q1: dt.date(2026, 3, 31), qi_q0: dt.date(2025, 12, 31)}
        }
        # Shares in Q1 = 1001, shares in Q0 = 1000 (delta = 1)
        shares_history = {
            (comp_id, cusip, False): {
                qi_q1: 1001,
                qi_q0: 1000,
            }
        }
        sq, _, _ = _calculate_streak(
            comp_id=comp_id,
            cusip=cusip,
            rep_period=rep_period,
            change_type="increased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        self.assertEqual(sq, 1, "Delta 1 is within cash-in-lieu tolerance, must stop at 1")

    def test_cash_in_lieu_boundary_delta_two(self):
        """Delta = 2: Beyond 1-share tolerance; qualifies as genuine share change, streak extends."""
        comp_id = 77773
        cusip = "037833100"
        rep_period = dt.date(2026, 6, 30)
        qi_q2 = quarter_index(rep_period)
        qi_q1 = quarter_index(dt.date(2026, 3, 31))
        qi_q0 = quarter_index(dt.date(2025, 12, 31))

        filed_quarters = {
            comp_id: {qi_q2: rep_period, qi_q1: dt.date(2026, 3, 31), qi_q0: dt.date(2025, 12, 31)}
        }
        # Shares in Q1 = 1002, shares in Q0 = 1000 (delta = 2 > 1)
        shares_history = {
            (comp_id, cusip, False): {
                qi_q1: 1002,
                qi_q0: 1000,
            }
        }
        sq, _, _ = _calculate_streak(
            comp_id=comp_id,
            cusip=cusip,
            rep_period=rep_period,
            change_type="increased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        self.assertEqual(sq, 2, "Delta 2 exceeds cash-in-lieu tolerance; streak extends to 2")

    # =========================================================================
    # 8. ADVERSARIAL FUZZING: MALFORMED & PATHOLOGICAL INPUTS
    # =========================================================================

    def test_fuzzing_change_type_casing_and_whitespace(self):
        """
        Adversarial fuzzing: change_type with mixed casing, leading/trailing spaces,
        newlines, or tabs must be normalized cleanly.
        """
        items = [
            {"cik": "0001067983", "cusip": "037833100", "change_type": "  NEW  ", "period_of_report": "2026-06-30"},
            {"cik": "0001067983", "cusip": "037833100", "change_type": "\tincreased\n", "period_of_report": "2026-06-30"},
            {"cik": "0001067983", "cusip": "037833100", "change_type": "DECREASED", "period_of_report": "2026-06-30"},
            {"cik": "0001067983", "cusip": "037833100", "change_type": " cLoSeD ", "period_of_report": "2026-06-30"},
            {"cik": "0001067983", "cusip": "037833100", "change_type": "  UnChanged  ", "period_of_report": "2026-06-30"},
        ]
        enrich_activities(self.conn, items)
        self.assertEqual(items[0]["streak_quarters"], 1)
        self.assertEqual(items[0]["streak_direction"], "buying")
        self.assertEqual(items[1]["streak_direction"], "buying")
        self.assertEqual(items[2]["streak_direction"], "selling")
        self.assertEqual(items[3]["streak_direction"], "selling")
        self.assertIsNone(items[4]["streak_direction"])
        self.assertIn(items[4]["streak_quarters"], (None, 0))

    def test_fuzzing_empty_and_unknown_change_types(self):
        """
        Adversarial fuzzing: empty, null, or completely unknown change_types
        must not crash and must maintain streak_direction == streak_type.
        """
        items = [
            {"change_type": "", "cusip": "037833100", "period_of_report": "2026-06-30"},
            {"change_type": None, "cusip": "037833100", "period_of_report": "2026-06-30"},
            {"change_type": "UNKNOWN_ACTION", "cusip": "037833100", "period_of_report": "2026-06-30"},
            {"change_type": "SPLIT", "cusip": "037833100", "period_of_report": "2026-06-30"},
        ]
        enrich_activities(self.conn, items)
        for act in items:
            self.assertEqual(
                act.get("streak_direction"),
                act.get("streak_type"),
                "Invariant violated on unknown change_type",
            )

    def test_fuzzing_malformed_dates_and_cusips(self):
        """
        Adversarial fuzzing: malformed date strings, None dates, empty CUSIPs,
        and SQL injection strings must be safely handled without database corruption.
        """
        items = [
            {"cik": "0001067983", "cusip": "037833100", "period_of_report": "invalid-date", "change_type": "increased"},
            {"cik": "0001067983", "cusip": "037833100", "period_of_report": None, "change_type": "increased"},
            {"cik": "0001067983", "cusip": None, "period_of_report": dt.date(2026, 6, 30), "change_type": "increased"},
            {"cik": "0001067983", "cusip": "'; DROP TABLE holdings; --", "period_of_report": dt.date(2026, 6, 30), "change_type": "increased"},
        ]
        enrich_activities(self.conn, items)
        for act in items:
            self.assertEqual(act.get("streak_direction"), act.get("streak_type"))
            self.assertGreaterEqual(act.get("streak_quarters", 0), 1)

    def test_fuzzing_pydantic_holding_activity_mutation(self):
        """
        Adversarial fuzzing: Ensure enrich_activities operates seamlessly on
        Pydantic HoldingActivity model instances.
        """
        models = [
            HoldingActivity(
                cik="0001067983",
                company_name="Berkshire",
                latest_accession_number="0001067983-26-000001",
                reporting_period=dt.date(2026, 6, 30),
                filing_date=dt.date(2026, 8, 14),
                issuer_name="Apple Inc.",
                cusip="037833100",
                is_common_stock=True,
                change_type="new",
                absolute_value_change=100,
            ),
            HoldingActivity(
                cik="0001067983",
                company_name="Berkshire",
                latest_accession_number="0001067983-26-000001",
                reporting_period=dt.date(2026, 6, 30),
                filing_date=dt.date(2026, 8, 14),
                issuer_name="Unchanged Holding",
                cusip="002824100",
                is_common_stock=True,
                change_type="unchanged",
                absolute_value_change=0,
            ),
        ]
        enrich_activities(self.conn, models)
        self.assertEqual(models[0].streak_quarters, 1)
        self.assertEqual(models[0].streak_direction, "buying")
        self.assertEqual(models[0].streak_type, "buying")
        self.assertIsNone(models[1].streak_quarters)
        self.assertIsNone(models[1].streak_direction)

    # =========================================================================
    # 9. INCEPTION AND DEPTH WALKBACK INVARIANTS
    # =========================================================================

    def test_walkback_inception_termination(self):
        """
        Inception walkback: When a position was initiated at Q_inception (shares from 0),
        the buying walkback must stop cleanly at inception and not decrement into negative quarters.
        """
        comp_id = 66661
        cusip = "037833100"
        rep_period = dt.date(2026, 9, 30) # Q3
        qi_q3 = quarter_index(rep_period)
        qi_q2 = quarter_index(dt.date(2026, 6, 30))
        qi_q1 = quarter_index(dt.date(2026, 3, 31))
        qi_q0 = quarter_index(dt.date(2025, 12, 31))

        filed_quarters = {
            comp_id: {
                qi_q3: rep_period,
                qi_q2: dt.date(2026, 6, 30),
                qi_q1: dt.date(2026, 3, 31),
                qi_q0: dt.date(2025, 12, 31),
            }
        }
        # In Q1 shares were 500; in Q0 shares were 0 (position started in Q1)
        shares_history = {
            (comp_id, cusip, False): {
                qi_q2: 1000,
                qi_q1: 500,
                qi_q0: 0,
            }
        }

        sq, sdir, stype = _calculate_streak(
            comp_id=comp_id,
            cusip=cusip,
            rep_period=rep_period,
            change_type="increased",
            is_option=False,
            shares_history=shares_history,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        # Q3 vs Q2: increased (base 1)
        # Q2 vs Q1: 1000 > 500 (+1 => 2)
        # Q1 vs Q0: 500 > 0 (+1 => 3), then s_prev == 0 triggers break!
        self.assertEqual(sq, 3, f"Streak should terminate at position inception with count 3, got {sq}")
        self.assertEqual(sdir, "buying")

    def test_walkback_maximum_depth_cap(self):
        """
        Walkback cap: Even if a position has been increasing for 20 quarters,
        the walkback is capped at 12 quarters (3 years) + 1 base = max 13.
        """
        comp_id = 66662
        cusip = "037833100"
        rep_period = dt.date(2026, 6, 30)
        qi_curr = quarter_index(rep_period)

        filed_quarters = {comp_id: {}}
        shares_hist = {(comp_id, cusip, False): {}}

        # Create 20 consecutive quarters of strictly increasing shares
        for i in range(25):
            qi = qi_curr - i
            d = dt.date(2026 - (i // 4), max(1, 3 * ((qi % 4) + 1)), 28)
            filed_quarters[comp_id][qi] = d
            shares_hist[(comp_id, cusip, False)][qi] = 100000 - i * 1000

        sq, sdir, _ = _calculate_streak(
            comp_id=comp_id,
            cusip=cusip,
            rep_period=rep_period,
            change_type="increased",
            is_option=False,
            shares_history=shares_hist,
            company_filed_quarters=filed_quarters,
            splits_map={},
        )
        self.assertGreaterEqual(sq, 10, "Should evaluate deep streak")
        self.assertLessEqual(sq, 13, "Should be bounded by maximum loop walkback")

    # =========================================================================
    # 10. SIMULATED EXCEPTION FALLBACK INVARIANT SAFETY
    # =========================================================================

    def test_simulated_db_failure_fallback_preserves_invariants(self):
        """
        Simulate a database failure inside enrich_activities:
        Verify that the exception handler executes gracefully, does not raise to caller,
        and ensures streak_direction == streak_type on all returned fallback activities.
        """
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = psycopg2.OperationalError("Simulated DB connection drop")

        items = [
            {"change_type": "new", "cusip": "037833100"},
            {"change_type": "increased", "cusip": "037833100"},
            {"change_type": "decreased", "cusip": "037833100"},
            {"change_type": "closed", "cusip": "037833100"},
            {"change_type": "unchanged", "cusip": "037833100"},
        ]

        # Should not raise exception
        result = enrich_activities(mock_cursor, items)
        self.assertEqual(len(result), 5)
        for act in result:
            ct = act.get("change_type")
            sdir = act.get("streak_direction")
            stype = act.get("streak_type")
            self.assertEqual(
                sdir,
                stype,
                f"Fallback failed invariant: sdir={sdir} != stype={stype}",
            )
            if ct == "new":
                self.assertEqual(act.get("streak_quarters"), 1)
                self.assertEqual(sdir, "buying")
            elif ct == "increased":
                self.assertEqual(act.get("streak_quarters"), 1)
                self.assertEqual(sdir, "buying")
            elif ct in ("decreased", "closed"):
                self.assertEqual(act.get("streak_quarters"), 1)
                self.assertEqual(sdir, "selling")
            elif ct == "unchanged":
                self.assertIn(act.get("streak_quarters"), (None, 0))
                self.assertIsNone(sdir)


if __name__ == "__main__":
    unittest.main()
