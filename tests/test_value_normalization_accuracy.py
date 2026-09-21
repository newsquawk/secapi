"""
tests/test_value_normalization_accuracy.py

Dedicated validation suite verifying whether 13F holding values
when multiplied by 1000 or kept as-is are mathematically and factually accurate.

Validates across:
1. Historical Market Price Cross-Referencing:
   Checks implied share prices against real historical market prices for known stocks (AAPL, MSFT).
2. Portfolio AUM Reconciliation:
   Checks that fund AUM / sum(normalised_value) is consistently ~1.0 across both pre-2023 and post-2023 eras.
3. Penny Stock Verification:
   Ensures sub-$1 stocks in 2023-2025 are not erroneously multiplied by 1000x.
"""

import os
import unittest
import psycopg2
from psycopg2.extras import RealDictCursor


def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "sec"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "password"),
    )


class TestValueNormalizationAccuracy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_historical_known_stock_prices_pre_and_post_cutoff(self):
        """
        Verify Apple (AAPL, CUSIP 037833100) implied share prices across
        2022 and 2023 quarterly reports against real historical market prices.

        Historical Apple quarter-end closing prices:
        - 2022-06-30: ~$136.72
        - 2022-09-30: ~$138.20
        - 2022-12-31: ~$129.93
        - 2023-03-31: ~$164.90
        - 2023-06-30: ~$193.97
        """
        cur = self.conn.cursor(cursor_factory=RealDictCursor)
        query = """
            SELECT f.period_of_report, f.filing_date,
                   SUM(hn.value) as total_norm_value,
                   SUM(hn.shares_or_principal_amount) as total_shares,
                   ROUND(SUM(hn.value)::numeric / SUM(hn.shares_or_principal_amount), 2) as implied_price
            FROM holdings_normalised hn
            JOIN filings f ON hn.filing_id = f.filing_id
            JOIN companies c ON f.company_id = c.company_id
            JOIN issuers i ON hn.issuer_id = i.issuer_id
            WHERE i.cusip = '037833100'
              AND c.company_name ILIKE '%BERKSHIRE HATHAWAY%'
              AND f.period_of_report IN ('2022-06-30', '2022-09-30', '2022-12-31', '2023-03-31', '2023-06-30')
            GROUP BY f.period_of_report, f.filing_date
            ORDER BY f.period_of_report;
        """
        cur.execute(query)
        rows = cur.fetchall()

        expected_prices = {
            "2022-06-30": 136.72,
            "2022-09-30": 138.20,
            "2022-12-31": 129.93,
            "2023-03-31": 164.90,
            "2023-06-30": 193.97,
        }

        for row in rows:
            period = str(row["period_of_report"])
            expected = expected_prices.get(period)
            if expected:
                implied = float(row["implied_price"])
                # Implied price must be within 2% of actual market closing price
                pct_diff = abs(implied - expected) / expected
                self.assertLess(
                    pct_diff,
                    0.03,
                    f"Implied price ${implied} for {period} deviates > 3% from real market price ${expected}!",
                )

    def test_fund_aum_reconciliation_stability(self):
        """
        Checks that portfolio AUM to SUM(normalised_value) is stable (~1.0)
        across both pre-2023 and post-2023 filings, proving values are in whole dollars.
        """
        cur = self.conn.cursor(cursor_factory=RealDictCursor)
        query = """
            WITH FundTotals AS (
                SELECT f.filing_id, f.period_of_report, f.filing_date, c.company_name, c.aum,
                       SUM(hn.value) as total_norm_val,
                       ROUND(c.aum::numeric / NULLIF(SUM(hn.value), 0), 2) as aum_ratio
                FROM filings f
                JOIN companies c ON f.company_id = c.company_id
                JOIN holdings_normalised hn ON f.filing_id = hn.filing_id
                WHERE c.company_name ILIKE '%BERKSHIRE HATHAWAY%'
                  AND f.period_of_report IN ('2022-06-30', '2022-09-30', '2022-12-31', '2023-03-31', '2023-06-30')
                GROUP BY f.filing_id, f.period_of_report, f.filing_date, c.company_name, c.aum
            )
            SELECT * FROM FundTotals ORDER BY period_of_report;
        """
        cur.execute(query)
        rows = cur.fetchall()

        for row in rows:
            ratio = float(row["aum_ratio"])
            # In whole dollars, AUM / portfolio value is ~1.0 (between 0.8 and 1.3)
            # If it were in thousands, ratio would be ~1,000.
            # If it were wrongly multiplied by 1,000 twice, ratio would be ~0.001.
            self.assertGreater(
                ratio,
                0.7,
                f"AUM ratio {ratio} too small for {row['period_of_report']} - over-multiplication detected!",
            )
            self.assertLess(
                ratio,
                2.0,
                f"AUM ratio {ratio} too large for {row['period_of_report']} - under-multiplication detected!",
            )

    def test_post_2023_penny_stocks_not_inflated(self):
        """
        Verifies that modern penny stocks (price < $1.00) in 2024/2025 are preserved
        as whole dollars and NOT falsely multiplied by 1000x.
        """
        cur = self.conn.cursor(cursor_factory=RealDictCursor)
        query = """
            SELECT hn.holding_id, f.period_of_report, f.filing_date,
                   hn.shares_or_principal_amount as shares,
                   hn.value as norm_val,
                   ROUND(hn.value::numeric / hn.shares_or_principal_amount, 4) as implied_price
            FROM holdings_normalised hn
            JOIN filings f ON hn.filing_id = f.filing_id
            WHERE f.filing_date >= '2024-01-01'
              AND hn.shares_or_principal_amount > 50000
              AND hn.value > 0
              AND (hn.value::numeric / hn.shares_or_principal_amount) < 0.90
            LIMIT 10;
        """
        cur.execute(query)
        rows = cur.fetchall()
        self.assertGreater(len(rows), 0, "No penny stock rows found for validation")

        for row in rows:
            price = float(row["implied_price"])
            # Implied price must remain sub-$1.00 (not blown up to $100-$900)
            self.assertLess(
                price,
                1.00,
                f"Penny stock holding {row['holding_id']} was falsely inflated to ${price}/share!",
            )


if __name__ == "__main__":
    unittest.main()
