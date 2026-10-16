-- scripts/011_amendment_flags.sql
-- Migration adding amendment_type column and indexing for 13F filing amendments.

ALTER TABLE filings ADD COLUMN IF NOT EXISTS amendment_type VARCHAR(30);
CREATE INDEX IF NOT EXISTS idx_filings_amendment_type ON filings (amendment_type);

-- Populate amendment_type for all existing amendment filings
WITH AmendmentData AS (
    SELECT 
        af.filing_id,
        CASE 
            WHEN bf.filing_id IS NULL THEN 'restatement'
            WHEN COALESCE(bf.reported_entry_total, bf_cnt.cnt, 0) >= 50 AND COALESCE(af.reported_entry_total, af_cnt.cnt, 0) < 10 THEN 'addition'
            WHEN COALESCE(bf.reported_entry_total, bf_cnt.cnt, 0) > 0 AND (COALESCE(af.reported_entry_total, af_cnt.cnt, 0)::numeric / COALESCE(bf.reported_entry_total, bf_cnt.cnt, 0)) < 0.25 THEN 'addition'
            WHEN af.reported_value_total IS NOT NULL AND bf.reported_value_total IS NOT NULL AND bf.reported_value_total > 0 AND (af.reported_value_total::numeric / bf.reported_value_total) < 0.25 AND COALESCE(af.reported_entry_total, af_cnt.cnt, 0) < 0.5 * COALESCE(bf.reported_entry_total, bf_cnt.cnt, 0) THEN 'addition'
            ELSE 'restatement'
        END AS derived_amendment_type
    FROM filings af
    LEFT JOIN LATERAL (
        SELECT filing_id, reported_entry_total, reported_value_total
        FROM filings bf
        WHERE bf.company_id = af.company_id
          AND bf.period_of_report = af.period_of_report
          AND bf.form_type = '13F-HR'
        ORDER BY bf.filing_date DESC, bf.filing_id DESC
        LIMIT 1
    ) bf ON TRUE
    LEFT JOIN LATERAL (
        SELECT count(*) AS cnt FROM holdings h WHERE h.filing_id = af.filing_id
    ) af_cnt ON (af.reported_entry_total IS NULL)
    LEFT JOIN LATERAL (
        SELECT count(*) AS cnt FROM holdings h WHERE h.filing_id = bf.filing_id
    ) bf_cnt ON (bf.filing_id IS NOT NULL AND bf.reported_entry_total IS NULL)
    WHERE af.form_type LIKE '%/A%'
)
UPDATE filings f
SET amendment_type = ad.derived_amendment_type
FROM AmendmentData ad
WHERE f.filing_id = ad.filing_id;
