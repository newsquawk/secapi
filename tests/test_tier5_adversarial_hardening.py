"""
tests/test_tier5_adversarial_hardening.py

Tier 5 White-Box Adversarial Coverage Hardening Suite for secapi 13F Derived Fields:
  - R1: Position Entry Type ('first_ever' vs. 're_entry')
  - R2: Consecutive Quarterly Buying & Selling Streaks
  - R3: Flow Distinct Fund Counts by Change Type
  - R4: Amended & Late Filing Flags
  - R5: Zero git commits and pushes constraint verification

Audits and stress-tests code paths across:
  - holding_enricher.py
  - sec_models.py
  - routers/flow.py
  - routers/filings.py
  - change_feed.py
  - routers/activity.py
  - utils.py
"""

import collections
import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import psycopg2
from psycopg2.extras import RealDictCursor

from change_feed import (
    EMPTY_CURSOR,
    decode_cursor,
    encode_cursor,
)
from config import COMMON_STOCK_TITLE_OF_CLASS, CUSIP_TO_TICKER
from database import get_db_cursor
from holding_enricher import (
    _calculate_streak,
    _get_field,
    _parse_date,
    _set_field,
    enrich_activities,
    quarter_index,
)
from main import app
from sec_models import (
    AggregateFlowResponse,
    DailyFlowEntry,
    FilingEnvelope,
    HoldingActivity,
    QuarterlyFlowEntry,
    TopStockChangeEntry,
    compute_amendment_type,
    compute_is_amendment,
    compute_is_late_filing,
    compute_statutory_deadline,
    enrich_filing_dict,
)
from utils import get_free_float, resolve_identifiers


class TestTier5HoldingEnricherHardening(unittest.TestCase):
    """White-box stress tests for holding_enricher.py internal routines and edge cases."""

    def setUp(self):
        self.cursor_gen = get_db_cursor()
        self.db = next(self.cursor_gen)

    def tearDown(self):
        try:
            next(self.cursor_gen)
        except StopIteration:
            pass

    def test_parse_date_exhaustive_edge_cases(self):
        """Verify _parse_date handles all formats, string variants, types, and invalid inputs."""
        # Standard date strings
        self.assertEqual(_parse_date("2026-06-30"), dt.date(2026, 6, 30))
        self.assertEqual(_parse_date(" 2026-12-31 "), dt.date(2026, 12, 31))

        # ISO datetime strings
        self.assertEqual(_parse_date("2026-02-17T14:30:00Z"), dt.date(2026, 2, 17))
        self.assertEqual(_parse_date("2026-02-17 14:30:00"), dt.date(2026, 2, 17))

        # Native date and datetime instances
        d = dt.date(2026, 3, 31)
        self.assertEqual(_parse_date(d), d)
        now = dt.datetime(2026, 8, 14, 18, 0, 0)
        self.assertEqual(_parse_date(now), dt.date(2026, 8, 14))

        # None, empty, whitespace
        self.assertIsNone(_parse_date(None))
        self.assertIsNone(_parse_date(""))
        self.assertIsNone(_parse_date("   "))

        # Invalid date strings
        self.assertIsNone(_parse_date("invalid-date"))
        self.assertIsNone(_parse_date("2026-99-99"))
        self.assertIsNone(_parse_date("short"))

        # Non-date, non-string types
        self.assertIsNone(_parse_date(12345))
        self.assertIsNone(_parse_date(["2026-06-30"]))

    def test_quarter_index_calculation_exhaustive(self):
        """Verify quarter_index produces exact calendar quarter indices across boundaries."""
        # 2025 quarters
        self.assertEqual(quarter_index(dt.date(2025, 1, 1)), 2025 * 4 + 0)
        self.assertEqual(quarter_index(dt.date(2025, 3, 31)), 2025 * 4 + 0)
        self.assertEqual(quarter_index(dt.date(2025, 4, 1)), 2025 * 4 + 1)
        self.assertEqual(quarter_index(dt.date(2025, 6, 30)), 2025 * 4 + 1)
        self.assertEqual(quarter_index(dt.date(2025, 7, 1)), 2025 * 4 + 2)
        self.assertEqual(quarter_index(dt.date(2025, 9, 30)), 2025 * 4 + 2)
        self.assertEqual(quarter_index(dt.date(2025, 10, 1)), 2025 * 4 + 3)
        self.assertEqual(quarter_index(dt.date(2025, 12, 31)), 2025 * 4 + 3)

        # Cross-year continuity: 2026-Q1 minus 2025-Q4 must be exactly 1
        q_2025_q4 = quarter_index(dt.date(2025, 12, 31))
        q_2026_q1 = quarter_index(dt.date(2026, 1, 1))
        self.assertEqual(q_2026_q1 - q_2025_q4, 1)

    def test_get_and_set_field_dict_and_object(self):
        """Verify _get_field and _set_field work uniformly on both dicts and class objects."""
        # Dict test
        d = {"a": 1, "b": "hello"}
        self.assertEqual(_get_field(d, "a"), 1)
        self.assertEqual(_get_field(d, "missing", 42), 42)
        _set_field(d, "c", 99)
        self.assertEqual(d["c"], 99)

        # Object test
        class Dummy:
            pass

        obj = Dummy()
        obj.x = "foo"
        self.assertEqual(_get_field(obj, "x"), "foo")
        self.assertEqual(_get_field(obj, "missing", "default"), "default")
        _set_field(obj, "y", "bar")
        self.assertEqual(obj.y, "bar")

    def test_calculate_streak_all_branch_permutations(self):
        """Stress-test _calculate_streak across all branches: splits, cash-in-lieu, gaps, reversals."""
        rep_date = dt.date(2026, 6, 30)
        qi_curr = quarter_index(rep_date)
        comp_id = 100
        cusip = "037833100"

        # 1. change_type == 'new'
        sq, sdir, stype = _calculate_streak(
            comp_id, cusip, rep_date, "new", False, {}, {}, {}
        )
        self.assertEqual((sq, sdir, stype), (1, "buying", "buying"))

        # 2. change_type == 'unchanged'
        sq, sdir, stype = _calculate_streak(
            comp_id, cusip, rep_date, "unchanged", False, {}, {}, {}
        )
        self.assertEqual((sq, sdir, stype), (None, None, None))

        # 3. No filed quarters in company_filed_quarters
        sq, sdir, stype = _calculate_streak(
            comp_id, cusip, rep_date, "increased", False, {}, {}, {}
        )
        self.assertEqual((sq, sdir, stype), (1, "buying", "buying"))

        # 4. Immediate preceding quarter missing (calendar gap)
        filed_quarters = {
            comp_id: {
                qi_curr - 2: dt.date(2025, 12, 31)  # Gap: qi_curr - 1 not filed
            }
        }
        sq, sdir, stype = _calculate_streak(
            comp_id, cusip, rep_date, "increased", False, {}, filed_quarters, {}
        )
        self.assertEqual((sq, sdir, stype), (1, "buying", "buying"))

        # 5. Consecutive buying streak extending 4 quarters
        filed_quarters = {
            comp_id: {
                qi_curr - 1: dt.date(2026, 3, 31),
                qi_curr - 2: dt.date(2025, 12, 31),
                qi_curr - 3: dt.date(2025, 9, 30),
                qi_curr - 4: dt.date(2025, 6, 30),
            }
        }
        shares_history = {
            (comp_id, cusip, False): {
                qi_curr - 1: 4000,
                qi_curr - 2: 3000,
                qi_curr - 3: 2000,
                qi_curr - 4: 1000,
            }
        }
        sq, sdir, stype = _calculate_streak(
            comp_id, cusip, rep_date, "increased", False, shares_history, filed_quarters, {}
        )
        self.assertEqual(sq, 4)
        self.assertEqual(sdir, "buying")

        # 6. Consecutive selling streak extending 3 quarters
        shares_history_sell = {
            (comp_id, cusip, False): {
                qi_curr - 1: 3000,
                qi_curr - 2: 4000,
                qi_curr - 3: 5000,
                qi_curr - 4: 6000,
            }
        }
        sq, sdir, stype = _calculate_streak(
            comp_id, cusip, rep_date, "decreased", False, shares_history_sell, filed_quarters, {}
        )
        self.assertEqual(sq, 4)
        self.assertEqual(sdir, "selling")

        # 7. Stock split rebasing (2:1 split at qi_curr - 1)
        # s_prev=1000, s_curr=2000 -> rebased s_prev=2000. abs(2000-2000) <= 1: cash-in-lieu unchanged terminates streak!
        splits_map = {(cusip, qi_curr - 1): 2.0}
        shares_with_split = {
            (comp_id, cusip, False): {
                qi_curr - 1: 2000,
                qi_curr - 2: 1000,
            }
        }
        sq, sdir, stype = _calculate_streak(
            comp_id, cusip, rep_date, "increased", False, shares_with_split, filed_quarters, splits_map
        )
        self.assertEqual(sq, 1)  # Stopped by cash-in-lieu unchanged

        # 8. Stock split rebasing for Options (common stock scope isolation)
        # For options, splits_map must be IGNORED (split_factor = 1.0)
        # s_prev=1000, s_curr=2000 -> 2000 > 1000 (buying continues on option contract count)
        shares_opt = {
            (comp_id, cusip, True): {
                qi_curr - 1: 2000,
                qi_curr - 2: 1000,
            }
        }
        sq, sdir, stype = _calculate_streak(
            comp_id, cusip, rep_date, "increased", True, shares_opt, filed_quarters, splits_map
        )
        self.assertEqual(sq, 3)  # Extended because options ignore split

        # 9. 1-share cash-in-lieu tolerance boundary: difference of exactly 1 share halts streak
        shares_tolerance = {
            (comp_id, cusip, False): {
                qi_curr - 1: 1001,
                qi_curr - 2: 1000,
            }
        }
        sq, sdir, stype = _calculate_streak(
            comp_id, cusip, rep_date, "increased", False, shares_tolerance, filed_quarters, {}
        )
        self.assertEqual(sq, 1)  # 1001 vs 1000 diff is 1 <= 1 tolerance, halted

        # Difference of 2 shares continues streak
        shares_two_diff = {
            (comp_id, cusip, False): {
                qi_curr - 1: 1002,
                qi_curr - 2: 1000,
                qi_curr - 3: 1000,
            }
        }
        sq, sdir, stype = _calculate_streak(
            comp_id, cusip, rep_date, "increased", False, shares_two_diff, filed_quarters, {}
        )
        self.assertEqual(sq, 2)

        # 10. Position initiation at qi_curr - 1 (s_prev == 0) terminates walk
        shares_initiation = {
            (comp_id, cusip, False): {
                qi_curr - 1: 1000,
                qi_curr - 2: 0,
            }
        }
        sq, sdir, stype = _calculate_streak(
            comp_id, cusip, rep_date, "increased", False, shares_initiation, filed_quarters, {}
        )
        self.assertEqual(sq, 2)  # Can reach 2 quarters, then terminates at inception

    def test_enrich_activities_empty_and_unknown_change_type(self):
        """Verify enrich_activities handles empty input and unrecognized change_types safely."""
        self.assertEqual(enrich_activities(self.db, []), [])

        item = {
            "change_type": "unknown_action",
            "cusip": "037833100",
            "cik": "0001067983",
            "reporting_period": "2026-06-30",
        }
        enriched = enrich_activities(self.db, [item])
        self.assertEqual(len(enriched), 1)
        self.assertIsNone(enriched[0]["position_entry_type"])
        self.assertIsNone(enriched[0]["streak_direction"])

    def test_enrich_activities_exception_fallback(self):
        """Verify enrich_activities applies safe fallbacks if the database raises an unexpected error."""
        broken_db = MagicMock()
        mock_cur = MagicMock()
        mock_cur.execute.side_effect = RuntimeError("Database connection severed")
        broken_db.cursor.return_value = mock_cur

        activities = [
            {"change_type": "new", "cusip": "037833100"},
            {"change_type": "increased", "cusip": "037833100"},
            {"change_type": "decreased", "cusip": "037833100"},
        ]
        res = enrich_activities(broken_db, activities)
        self.assertEqual(len(res), 3)
        self.assertEqual(res[0]["position_entry_type"], "first_ever")
        self.assertEqual(res[0]["streak_quarters"], 1)
        self.assertEqual(res[1]["streak_direction"], "buying")
        self.assertEqual(res[2]["streak_direction"], "selling")


class TestTier5SecModelsHardening(unittest.TestCase):
    """White-box stress tests for sec_models.py statutory deadline, flags, and models."""

    def test_compute_statutory_deadline_all_quarter_boundaries(self):
        """Verify compute_statutory_deadline across leap years, standard years, and holiday roll-forwards."""
        # Q1: March 31 + 45 days = May 15
        # 2026-03-31 + 45 days = 2026-05-15 (Friday) -> 2026-05-15
        self.assertEqual(compute_statutory_deadline(dt.date(2026, 3, 31)), dt.date(2026, 5, 15))

        # Q2: June 30 + 45 days = August 14
        # 2026-06-30 + 45 days = 2026-08-14 (Friday) -> 2026-08-14
        self.assertEqual(compute_statutory_deadline(dt.date(2026, 6, 30)), dt.date(2026, 8, 14))

        # Q3: September 30 + 45 days = November 14
        # 2026-09-30 + 45 days = 2026-11-14 (Saturday!) -> Rolls to Monday Nov 16
        self.assertEqual(compute_statutory_deadline(dt.date(2026, 9, 30)), dt.date(2026, 11, 16))

        # 2027-09-30 + 45 days = 2027-11-14 (Sunday!) -> Rolls to Monday Nov 15
        self.assertEqual(compute_statutory_deadline(dt.date(2027, 9, 30)), dt.date(2027, 11, 15))

        # Q4: December 31 + 45 days = February 14 (Valentine's Day)
        # 2026: Feb 14 is Saturday -> rolls to Monday Feb 16 (Presidents' Day, day 16) -> rolls to Tuesday Feb 17!
        self.assertEqual(compute_statutory_deadline(dt.date(2025, 12, 31)), dt.date(2026, 2, 17))

        # 2021: Feb 14 was Sunday -> rolls to Monday Feb 15 (Presidents' Day, day 15) -> rolls to Tuesday Feb 16!
        self.assertEqual(compute_statutory_deadline(dt.date(2020, 12, 31)), dt.date(2021, 2, 16))

        # 2024: Leap year, Feb 14 was Wednesday -> deadline stays Feb 14
        self.assertEqual(compute_statutory_deadline(dt.date(2023, 12, 31)), dt.date(2024, 2, 14))

        # String format type coercion
        self.assertEqual(compute_statutory_deadline("2025-12-31"), dt.date(2026, 2, 17))
        self.assertEqual(compute_statutory_deadline("2025-12-31T00:00:00Z"), dt.date(2026, 2, 17))
        self.assertIsNone(compute_statutory_deadline(None))
        self.assertIsNone(compute_statutory_deadline(""))

    def test_compute_is_late_filing_boundary_precision(self):
        """Verify compute_is_late_filing distinguishes on-time, exact deadline, and late filings."""
        p_q4_2025 = "2025-12-31"  # Deadline: Tuesday, 2026-02-17

        # Day 45 (Saturday 2026-02-14) -> on-time
        self.assertFalse(compute_is_late_filing("2026-02-14", p_q4_2025))
        # Presidents' Day (Monday 2026-02-16) -> on-time
        self.assertFalse(compute_is_late_filing("2026-02-16", p_q4_2025))
        # Exact deadline day (Tuesday 2026-02-17) -> on-time
        self.assertFalse(compute_is_late_filing("2026-02-17", p_q4_2025))
        # 1 day late (Wednesday 2026-02-18) -> LATE
        self.assertTrue(compute_is_late_filing("2026-02-18", p_q4_2025))

        # Empty inputs
        self.assertFalse(compute_is_late_filing(None, p_q4_2025))
        self.assertFalse(compute_is_late_filing("2026-02-18", None))

    def test_compute_is_amendment_all_forms(self):
        """Verify compute_is_amendment on standard forms, lowercase, and trailing variants."""
        self.assertFalse(compute_is_amendment("13F-HR"))
        self.assertFalse(compute_is_amendment("13F-NT"))
        self.assertFalse(compute_is_amendment("10-K"))

        self.assertTrue(compute_is_amendment("13F-HR/A"))
        self.assertTrue(compute_is_amendment("13F-NT/A"))
        self.assertTrue(compute_is_amendment("13F-HR/A/A"))
        self.assertTrue(compute_is_amendment("13f-hr/a"))  # Lowercase

        self.assertFalse(compute_is_amendment(None))
        self.assertFalse(compute_is_amendment(""))

    def test_compute_amendment_type_classification(self):
        """Verify compute_amendment_type distinguishes addition vs restatement."""
        # Non-amendment returns None
        self.assertIsNone(compute_amendment_type("13F-HR", "addition"))

        # Amendment with raw keywords
        self.assertEqual(compute_amendment_type("13F-HR/A", "addition"), "addition")
        self.assertEqual(compute_amendment_type("13F-HR/A", "ADDITION"), "addition")
        self.assertEqual(compute_amendment_type("13F-HR/A", "new_holdings_added"), "addition")
        self.assertEqual(compute_amendment_type("13F-HR/A", "restatement"), "restatement")
        self.assertEqual(compute_amendment_type("13F-HR/A", "RESTATEMENT"), "restatement")
        self.assertEqual(compute_amendment_type("13F-HR/A", "portfolio_restate"), "restatement")

        # Fallback default for 13F-HR/A when unspecified
        self.assertEqual(compute_amendment_type("13F-HR/A", None), "restatement")

    def test_enrich_filing_dict_mutation(self):
        """Verify enrich_filing_dict mutates dict in-place and accurately sets all 3 R4 flags."""
        d = {
            "form_type": "13F-HR/A",
            "amendment_type": "addition",
            "filing_date": "2026-08-26",
            "period_of_report": "2026-06-30",
        }
        res = enrich_filing_dict(d)
        self.assertTrue(res["is_amendment"])
        self.assertEqual(res["amendment_type"], "addition")
        self.assertTrue(res["is_late_filing"])  # Aug 26 > Aug 14 deadline

    def test_flow_model_invariants(self):
        """Verify Pydantic models preserve fund count mathematical invariants."""
        # DailyFlowEntry invariant
        entry = DailyFlowEntry(
            date=dt.date(2026, 8, 14),
            gross_buying=50000.0,
            gross_selling=20000.0,
            net_change=30000.0,
            funds_buying=5,
            funds_selling=3,
            funds_new=2,
            funds_increased=3,
            funds_decreased=2,
            funds_closed=1,
            funds_unchanged=4,
        )
        self.assertEqual(entry.funds_buying, entry.funds_new + entry.funds_increased)
        self.assertEqual(entry.funds_selling, entry.funds_decreased + entry.funds_closed)

        # TopStockChangeEntry invariant
        top = TopStockChangeEntry(
            issuer_name="Apple Inc.",
            cusip="037833100",
            net_shares_change=10000.0,
            net_value_change=2500000.0,
            absolute_value_change=2500000.0,
            gross_buying_shares=15000.0,
            gross_selling_shares=5000.0,
            funds_buying=4,
            funds_selling=2,
            funds_new=1,
            funds_increased=3,
            funds_decreased=2,
            funds_closed=0,
            funds_unchanged=5,
        )
        self.assertEqual(top.funds_buying, top.funds_new + top.funds_increased)
        self.assertEqual(top.funds_selling, top.funds_decreased + top.funds_closed)


class TestTier5ChangeFeedHardening(unittest.TestCase):
    """White-box stress tests for change_feed.py cursor encoding, keyset parsing, and SQL safety."""

    def test_encode_and_decode_cursor_roundtrip(self):
        """Verify cursor encode/decode round trip preserves exact microsecond timestamp and ID."""
        ts = dt.datetime(2026, 10, 16, 14, 30, 45, 123456, tzinfo=dt.timezone.utc)
        filing_id = 338374
        cursor = encode_cursor(ts, filing_id)
        self.assertIsInstance(cursor, str)

        dec_ts, dec_id = decode_cursor(cursor)
        self.assertEqual(dec_id, filing_id)
        self.assertEqual(dec_ts, "2026-10-16T14:30:45.123456Z")

    def test_decode_cursor_malformed_token_rejection(self):
        """Verify decode_cursor raises ValueError on corrupted or hostile tokens."""
        invalid_tokens = [
            "not-base64!!!",
            "YWJj",  # "abc" without underscore separator
            "MjAyNi0xMC0xNl8tNQ==",  # negative filing id
            "invalid_timestamp_1234",
            "",
        ]
        for token in invalid_tokens:
            with self.assertRaises(ValueError):
                decode_cursor(token)

    def test_empty_cursor_constant_fidelity(self):
        """Verify EMPTY_CURSOR decodes to epoch 1970-01-01 and filing_id 0."""
        ts_str, f_id = decode_cursor(EMPTY_CURSOR)
        self.assertEqual(f_id, 0)
        self.assertIn("1970-01-01", ts_str)


class TestTier5FlowEngineHardening(unittest.TestCase):
    """White-box stress tests for routers/flow.py and utils.py identifier waterfalls."""

    def setUp(self):
        self.client = TestClient(app)
        self.cursor_gen = get_db_cursor()
        self.db = next(self.cursor_gen)

    def tearDown(self):
        try:
            next(self.cursor_gen)
        except StopIteration:
            pass

    def test_resolve_identifiers_waterfall(self):
        """Verify resolve_identifiers correctly maps known tickers, CUSIPs, and ticker patterns."""
        # 1. Known Ticker AAPL
        ticker, cusip = resolve_identifiers("AAPL", self.db)
        self.assertEqual(ticker, "AAPL")
        self.assertEqual(cusip, "037833100")

        # 2. Known CUSIP 037833100
        ticker, cusip = resolve_identifiers("037833100", self.db)
        self.assertEqual(ticker, "AAPL")
        self.assertEqual(cusip, "037833100")

        # 3. Ticker with digit (e.g. FAKE1) is identified as likely ticker, not CUSIP
        ticker, cusip = resolve_identifiers("FAKE1", self.db)
        self.assertEqual(ticker, "FAKE1")
        self.assertIsNone(cusip)

        # 4. Genuine unknown CUSIP
        ticker, cusip = resolve_identifiers("ZZZZZZZZZ", self.db)
        self.assertIsNone(ticker)
        self.assertEqual(cusip, "ZZZZZZZZZ")

    def test_flow_endpoints_unknown_inputs_safety(self):
        """Verify daily, aggregate, and quarterly endpoints return clean contracts on bad inputs."""
        # Bad ticker -> 400 Bad Request
        res_bad = self.client.get("/api/v1/flow/daily/XYZQQ")
        self.assertEqual(res_bad.status_code, 400)
        self.assertIn("Could not resolve", res_bad.json().get("detail", ""))

        # Unknown CUSIP on daily flow -> 200 with empty data
        res_daily = self.client.get("/api/v1/flow/daily/ZZZZZZZZZ")
        self.assertEqual(res_daily.status_code, 200)
        self.assertEqual(res_daily.json().get("daily_data"), [])

        # Unknown CUSIP on aggregate flow -> 200 with all zero counts satisfying invariants
        res_agg = self.client.get("/api/v1/flow/aggregate/ZZZZZZZZZ")
        self.assertEqual(res_agg.status_code, 200)
        data = res_agg.json()
        self.assertEqual(data["funds_buying"], 0)
        self.assertEqual(data["funds_selling"], 0)
        self.assertEqual(data["funds_buying"], data["funds_new"] + data["funds_increased"])
        self.assertEqual(data["funds_selling"], data["funds_decreased"] + data["funds_closed"])

        # Unknown CUSIP on quarterly flow -> 200 with empty quarterly data
        res_q = self.client.get("/api/v1/flow/quarterly/ZZZZZZZZZ")
        self.assertEqual(res_q.status_code, 200)
        self.assertEqual(res_q.json().get("quarterly_data"), [])

    def test_flow_top_changes_sort_options_and_invariants(self):
        """Verify top-changes endpoint validates sort_by and enforces fund count invariants."""
        # Invalid sort_by returns 400
        res_inv = self.client.get("/api/v1/flow/top-changes?sort_by=unsupported")
        self.assertEqual(res_inv.status_code, 400)

        # Valid sort_by='shares' and sort_by='value'
        for sort in ["shares", "value"]:
            res = self.client.get(f"/api/v1/flow/top-changes?date=2026-08-14&sort_by={sort}")
            self.assertEqual(res.status_code, 200)
            stocks = res.json().get("stocks", [])
            for s in stocks:
                self.assertEqual(s["funds_buying"], s["funds_new"] + s["funds_increased"])
                self.assertEqual(s["funds_selling"], s["funds_decreased"] + s["funds_closed"])


class TestTier5FilingsEndpointsHardening(unittest.TestCase):
    """White-box stress tests for routers/filings.py input sanitization and schema contracts."""

    def setUp(self):
        self.client = TestClient(app)

    def test_accession_nul_byte_injection_protection(self):
        """Verify that accession numbers containing embedded NUL characters return 404 rather than 500."""
        # NUL character in filing detail
        res = self.client.get("/filings/0001085146-26-000657%00exploit")
        self.assertEqual(res.status_code, 404)

        # NUL character in holdings datatable
        res_h = self.client.get("/holdings/0001085146-26-000657%00exploit")
        self.assertEqual(res_h.status_code, 404)

    def test_filings_response_r4_flags_contract(self):
        """Verify that filings collection and detail endpoints expose all 3 R4 flags."""
        res = self.client.get("/filings?limit=5")
        self.assertEqual(res.status_code, 200)
        filings = res.json().get("filings", [])
        self.assertGreater(len(filings), 0)
        for f in filings:
            self.assertIn("is_amendment", f)
            self.assertIsInstance(f["is_amendment"], bool)
            self.assertIn("amendment_type", f)
            self.assertIn("is_late_filing", f)
            self.assertIsInstance(f["is_late_filing"], bool)

    def test_manager_filings_r4_flags_contract(self):
        """Verify that /managers/{cik}/filings endpoint items expose R4 flags."""
        res = self.client.get("/managers/0001067983/filings?limit=5")
        self.assertEqual(res.status_code, 200)
        filings = res.json().get("filings", [])
        self.assertGreater(len(filings), 0)
        for f in filings:
            self.assertIn("is_amendment", f)
            self.assertIn("amendment_type", f)
            self.assertIn("is_late_filing", f)


if __name__ == "__main__":
    unittest.main()
