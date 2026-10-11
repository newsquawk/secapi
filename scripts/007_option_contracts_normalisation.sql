-- ============================================================================
-- Migration: 007_option_contracts_normalisation.sql
-- Purpose: Normalise option share counts mistakenly reported in contracts
--          instead of underlying shares by SEC Form 13F filers.
--
-- Background:
-- Under SEC Form 13F rules, option holdings (PUT / CALL) must be reported as
-- the number of underlying shares (Contracts x 100).
-- A minority of filers (e.g. CTC LLC, BNP Paribas) erroneously report the raw
-- number of contracts in `shares_or_principal_amount`, while reporting full
-- notional value in `value`. This causes calculated price-per-share (value / shares)
-- to be ~100x too high (e.g. NVDA puts at $20,009 instead of $200.09).
--
-- Solution:
-- 1. Create a `reference_prices` table that stores the median price of common stock
--    lines per CUSIP and reporting period across all peer filers.
-- 2. Update `holdings_normalised` view:
--    If an option (PUT or CALL) has an implied price (value / shares) within 15%
--    of 100x the peer median reference price, scale `shares_or_principal_amount`
--    by 100.
-- 3. Expose `shares_raw` and `shares_adjusted` audit columns for full transparency.
-- ============================================================================

-- 1. Create reference_prices table
CREATE TABLE IF NOT EXISTS reference_prices (
    issuer_id INT NOT NULL,
    cusip VARCHAR(9) NOT NULL,
    period_of_report DATE NOT NULL,
    median_price NUMERIC(14, 4) NOT NULL,
    sample_size INT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (issuer_id, period_of_report)
);

CREATE INDEX IF NOT EXISTS idx_ref_prices_cusip_period 
    ON reference_prices (cusip, period_of_report);

CREATE INDEX IF NOT EXISTS idx_ref_prices_period 
    ON reference_prices (period_of_report);

-- 2. Update holdings_normalised view with contract scaling rule
CREATE OR REPLACE VIEW holdings_normalised AS
SELECT
    h.holding_id,
    h.filing_id,
    h.issuer_id,
    -- Normalized shares: scales by 100 if filer reported option contracts
    CASE
        WHEN h.put_or_call IN (2, 3, 4, 5) -- PUT or CALL
         AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1) -- SH (not PRN)
         AND rp.sample_size >= 3
         AND ABS((
             (CASE WHEN f.filing_date < '2023-01-03'::date THEN h.value * 1000 ELSE h.value END)::numeric 
             / NULLIF(h.shares_or_principal_amount, 0)
         ) - (100 * rp.median_price)) <= (0.15 * (100 * rp.median_price))
        THEN (h.shares_or_principal_amount::bigint * 100)
        ELSE h.shares_or_principal_amount
    END AS shares_or_principal_amount,
    h.shares_or_principal_type,
    -- Normalized value: pre-2023 filings in $1,000s scaled by 1,000
    CASE
        WHEN f.filing_date < '2023-01-03'::date THEN h.value * 1000
        ELSE h.value
    END AS value,
    h.title_of_class,
    h.put_or_call,
    h.investment_discretion,
    h.voting_authority_sole,
    h.voting_authority_shared,
    h.voting_authority_none,
    -- Audit columns for transparency and debugging
    h.shares_or_principal_amount AS shares_raw,
    CASE
        WHEN h.put_or_call IN (2, 3, 4, 5)
         AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1)
         AND rp.sample_size >= 3
         AND ABS((
             (CASE WHEN f.filing_date < '2023-01-03'::date THEN h.value * 1000 ELSE h.value END)::numeric 
             / NULLIF(h.shares_or_principal_amount, 0)
         ) - (100 * rp.median_price)) <= (0.15 * (100 * rp.median_price))
        THEN true
        ELSE false
    END AS shares_adjusted
FROM holdings h
JOIN filings f ON h.filing_id = f.filing_id
LEFT JOIN reference_prices rp 
    ON h.issuer_id = rp.issuer_id 
   AND f.period_of_report = rp.period_of_report;
