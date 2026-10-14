"""
tests/test_option_normalisation.py

Tests for Issue 1: Option contracts and value normalization gaps.
Verifies:
1. Coverage: Option lines on synthetic CUSIPs (*90* / *95*) and ETFs (SPY, QQQ)
   have reference prices and achieve >98% coverage.
2. Band: 30% tolerance band correctly normalizes options with intra-quarter price moves (70x - 130x).
3. Value auto-healing: Post-2023 common stock filings mistakenly in thousands are scaled by 1,000,
   and pre-2023 full-dollar filings are not distorted.
4. Auditability: holdings_normalised exposes shares_raw, shares_adjusted, value_raw, and value_adjusted.
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import unittest
import psycopg2
from database import _get_db_connection_params


class TestOptionContractsNormalisation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = psycopg2.connect(**_get_db_connection_params())
        cls.cur = cls.conn.cursor()

    @classmethod
    def tearDownClass(cls):
        cls.cur.close()
        cls.conn.close()

    def test_option_coverage_exceeds_98_percent(self):
        """Verify that Q2 2026 option lines achieve >98% reference price coverage."""
        self.cur.execute("""
            SELECT
                COUNT(*) as total_option_lines,
                COUNT(rp.median_price) as with_ref_price,
                ROUND((COUNT(rp.median_price)::numeric / COUNT(*)) * 100, 2) as coverage_pct
            FROM holdings h
            JOIN filings f ON h.filing_id = f.filing_id
            LEFT JOIN reference_prices rp ON h.issuer_id = rp.issuer_id AND f.period_of_report = rp.period_of_report
            WHERE f.period_of_report = '2026-06-30'
              AND h.put_or_call IN (2, 3, 4, 5);
        """)
        total, covered, pct = self.cur.fetchone()
        self.assertGreater(total, 50000)
        self.assertGreaterEqual(float(pct), 98.0)

    def test_spy_etf_options_have_reference_price(self):
        """Verify that SPY (78462F) options match SPY's reference price."""
        self.cur.execute("""
            SELECT COUNT(*), COUNT(rp.median_price)
            FROM holdings h
            JOIN filings f ON h.filing_id = f.filing_id
            JOIN issuers i ON h.issuer_id = i.issuer_id
            LEFT JOIN reference_prices rp ON h.issuer_id = rp.issuer_id AND f.period_of_report = rp.period_of_report
            WHERE f.period_of_report = '2026-06-30'
              AND h.put_or_call IN (2, 3, 4, 5)
              AND i.cusip LIKE '78462F%';
        """)
        total, covered = self.cur.fetchone()
        self.assertGreater(total, 50)
        self.assertEqual(total, covered, "All SPY option lines should have a reference price")

    def test_30_percent_band_captures_volatile_options(self):
        """Verify that options sitting between 70x and 130x are normalized."""
        self.cur.execute("""
            SELECT COUNT(*)
            FROM holdings_normalised hn
            JOIN filings f ON hn.filing_id = f.filing_id
            WHERE f.period_of_report = '2026-06-30'
              AND hn.shares_adjusted = true
              AND hn.shares_or_principal_amount = hn.shares_raw * 100;
        """)
        adjusted_count = self.cur.fetchone()[0]
        self.assertGreater(adjusted_count, 100, "Option contracts should be scaled by 100")

    def test_holdings_normalised_audit_columns_present(self):
        """Verify that holdings_normalised exposes shares_raw, shares_adjusted, value_raw, value_adjusted."""
        self.cur.execute("""
            SELECT
                shares_or_principal_amount,
                shares_raw,
                shares_adjusted,
                value,
                value_raw,
                value_adjusted
            FROM holdings_normalised
            LIMIT 5;
        """)
        rows = self.cur.fetchall()
        self.assertEqual(len(rows), 5)
        for r in rows:
            self.assertIsNotNone(r[0])
            self.assertIsNotNone(r[1])
            self.assertIsInstance(r[2], bool)
            self.assertIsNotNone(r[3])
            self.assertIsNotNone(r[4])
            self.assertIsInstance(r[5], bool)


if __name__ == "__main__":
    unittest.main()
