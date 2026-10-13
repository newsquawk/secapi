-- ============================================================================
-- Migration: 009_aum_filing_totals.sql
-- Purpose: Support per-filing reported value totals and repair legacy
--          company AUM distortions (Issue 3 from audit report).
--
-- Background:
-- 1. Previously, `/filings` and `/api/v1/search/filings_by_aum` stamped all
--    historical filings with the company's single latest `companies.aum`.
-- 2. SEC rules prior to 2023-01-03 required holdings values in thousands ($1000s).
--    ~103 inactive managers whose latest filing was before 2023 had their
--    `companies.aum` stored in thousands instead of full dollars (e.g. BlackRock
--    Fund Advisors $542M vs true $542B).
-- 3. Ingestion of small partial amendments (13F-HR/A with < 10 holdings)
--    previously overwrote `companies.aum` with the amendment's small total
--    instead of the base filing's portfolio total (e.g. HighTower Advisors $943M
--    vs true $103.1B base).
--
-- Solution:
-- 1. Ensure `reported_value_total` and `reported_entry_total` columns exist on `filings`.
-- 2. Add performance index on `filings (reported_value_total)`.
-- 3. Repair legacy pre-2023 companies stored in thousands (sub-second indexed query).
-- 4. Restore companies corrupted by small partial amendments back to the base filing total.
-- ============================================================================

-- Disable statement timeout for the duration of this migration
SET statement_timeout = 0;

-- Step 1: Ensure columns exist on `filings`
ALTER TABLE filings ADD COLUMN IF NOT EXISTS reported_value_total BIGINT;
ALTER TABLE filings ADD COLUMN IF NOT EXISTS reported_entry_total INT;

-- Step 2: Index for efficient AUM filtering and sorting
CREATE INDEX IF NOT EXISTS idx_filings_reported_value_total 
    ON filings (reported_value_total);

-- Step 3: Repair pre-2023 companies whose latest filing had aum stored in thousands ($1,000s)
WITH Pre2023Candidates AS (
    SELECT c.company_id
    FROM companies c
    WHERE c.aum > 0
      AND NOT EXISTS (
          SELECT 1 FROM filings f
          WHERE f.company_id = c.company_id
            AND f.filing_date >= '2023-01-01'
      )
      AND EXISTS (
          SELECT 1 FROM filings f
          WHERE f.company_id = c.company_id
      )
),
LatestFilings AS (
    SELECT DISTINCT ON (f.company_id)
        f.company_id, f.filing_id
    FROM filings f
    JOIN Pre2023Candidates p ON f.company_id = p.company_id
    ORDER BY f.company_id, f.filing_date DESC, f.filing_id DESC
),
NormSums AS (
    SELECT
        lf.company_id,
        SUM(h.value * 1000) AS norm_sum
    FROM LatestFilings lf
    JOIN holdings h ON lf.filing_id = h.filing_id
    GROUP BY lf.company_id
)
UPDATE companies c
SET aum = ns.norm_sum
FROM NormSums ns
WHERE c.company_id = ns.company_id
  AND c.aum < ns.norm_sum / 500;

-- Step 4: Repair companies whose AUM was corrupted by small partial amendments (13F-HR/A)
WITH LatestFiling AS (
    SELECT DISTINCT ON (company_id)
        filing_id, company_id, form_type, filing_date, period_of_report
    FROM filings
    ORDER BY company_id, filing_date DESC, filing_id DESC
),
AmendedCandidates AS (
    SELECT lf.filing_id, lf.company_id, lf.period_of_report
    FROM LatestFiling lf
    WHERE lf.form_type LIKE '%/A%'
      AND (SELECT COUNT(*) FROM holdings WHERE filing_id = lf.filing_id) < 10
),
BaseFilingSamePeriod AS (
    SELECT DISTINCT ON (f.company_id)
        f.company_id, f.filing_id, f.filing_date
    FROM filings f
    JOIN AmendedCandidates ac ON f.company_id = ac.company_id
    WHERE f.period_of_report = ac.period_of_report
      AND f.form_type = '13F-HR'
    ORDER BY f.company_id, f.filing_date DESC, f.filing_id DESC
),
BaseHoldingSums AS (
    SELECT
        bf.company_id,
        SUM(CASE WHEN bf.filing_date < '2023-01-03' THEN h.value * 1000 ELSE h.value END) AS base_portfolio_val
    FROM BaseFilingSamePeriod bf
    JOIN holdings h ON bf.filing_id = h.filing_id
    GROUP BY bf.company_id
    HAVING COUNT(h.holding_id) >= 50
)
UPDATE companies c
SET aum = bhs.base_portfolio_val
FROM BaseHoldingSums bhs
WHERE c.company_id = bhs.company_id
  AND bhs.base_portfolio_val > c.aum * 2;
