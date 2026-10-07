"""
tests/test_change_feed_share_types.py

Runs change_feed.fetch_changes against a throwaway embedded Postgres (pgserver).
SH (share count) and PRN (principal dollars) lines on the same CUSIP must be
aggregated and diffed separately, and PRN rows must carry no price per share.
Skipped when pgserver is not installed.
"""

import os
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DB_PASSWORD", "password")

import tempfile
import unittest

try:
    import pgserver
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:  # pragma: no cover
    pgserver = None

import change_feed

SCHEMA = """
CREATE TABLE companies (company_id INT PRIMARY KEY, company_name TEXT, cik_number TEXT, aum BIGINT);
CREATE TABLE filings (filing_id INT PRIMARY KEY, company_id INT, accession_number TEXT,
    period_of_report DATE, filing_date DATE, created_at TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT now() - interval '1 day', form_type TEXT DEFAULT '13F-HR',
    file_number TEXT, filing_directory TEXT);
CREATE TABLE issuers (issuer_id INT PRIMARY KEY, cusip TEXT, issuer_name TEXT);
CREATE TABLE title_of_class_table (id INT PRIMARY KEY, name TEXT, is_common_stock BOOLEAN);
CREATE TABLE put_or_call_table (id INT PRIMARY KEY, name TEXT);
CREATE TABLE share_type_table (id INT PRIMARY KEY, name TEXT);
CREATE TABLE holdings_normalised (filing_id INT, issuer_id INT, title_of_class INT,
    put_or_call INT, shares_or_principal_type INT, shares_or_principal_amount BIGINT, value BIGINT);

INSERT INTO companies VALUES (1, 'Acme Capital', '0000000001', 1000000000);
INSERT INTO issuers VALUES (1, 'ABC123456', 'ABC CORP');
INSERT INTO title_of_class_table VALUES (1, 'COM', TRUE);
INSERT INTO share_type_table VALUES (1, 'SH'), (2, 'PRN');
-- previous quarter (filing 1) and latest (filing 2)
INSERT INTO filings (filing_id, company_id, accession_number, period_of_report, filing_date, created_at)
VALUES (1, 1, 'ACC-PREV', '2026-03-31', '2026-05-10', now()),
       (2, 1, 'ACC-LATEST', '2026-06-30', '2026-08-10', now());
-- previous: 1,000 shares ($100k) + $5,000,000 principal on the same CUSIP
INSERT INTO holdings_normalised VALUES (1, 1, 1, NULL, 1, 1000, 100000), (1, 1, 1, NULL, 2, 5000000, 5000000);
-- latest: 1,500 shares ($150k) + $2,000,000 principal
INSERT INTO holdings_normalised VALUES (2, 1, 1, NULL, 1, 1500, 150000), (2, 1, 1, NULL, 2, 2000000, 2000000);
"""


@unittest.skipUnless(pgserver, "pgserver not installed")
class TestChangeFeedShareTypes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = pgserver.get_server(cls.tmp.name)
        cls.conn = psycopg2.connect(cls.srv.get_uri())
        cls.conn.autocommit = True
        cls.conn.cursor().execute(SCHEMA)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.srv.cleanup()
        cls.tmp.cleanup()

    def _activities(self):
        cur = self.conn.cursor(cursor_factory=RealDictCursor)
        items, _, _ = change_feed.fetch_changes(cur, None, 1, 0, "backward")
        self.assertEqual(items[0].accession_number, "ACC-LATEST")
        return {a.shares_or_principal_type: a for a in items[0].activities}

    def test_sh_and_prn_are_separate_rows(self):
        acts = self._activities()
        self.assertEqual(set(acts), {"SH", "PRN"})

    def test_units_are_not_summed_or_diffed_together(self):
        acts = self._activities()
        self.assertEqual(
            (acts["SH"].current_shares, acts["SH"].previous_shares, acts["SH"].change_type),
            (1500, 1000, "increased"),
        )
        self.assertEqual(
            (acts["PRN"].current_shares, acts["PRN"].previous_shares, acts["PRN"].change_type),
            (2000000, 5000000, "decreased"),
        )

    def test_prn_has_no_price_per_share(self):
        acts = self._activities()
        self.assertEqual(acts["SH"].current_price_per_share, 100.0)
        self.assertIsNone(acts["PRN"].current_price_per_share)
        self.assertIsNone(acts["PRN"].previous_price_per_share)


if __name__ == "__main__":
    unittest.main()
