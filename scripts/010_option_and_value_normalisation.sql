-- ============================================================================
-- Migration: 010_option_and_value_normalisation.sql
-- Purpose: Complete options normalization and resolve remaining coverage gaps
--          (Issue 1 from validation report):
--          1. Widen option contract detection tolerance band from 15% to 30%
--             to capture volatile intra-quarter positions (70x to 130x median).
--          2. Auto-heal the ÷1000 common stock cluster (filers submitting in
--             $1000s post-2023, and protecting full-dollar pre-2023 filings).
--          3. Provide audit columns (shares_raw, shares_adjusted, value_raw, value_adjusted).
-- ============================================================================

SET statement_timeout = 0;

CREATE OR REPLACE VIEW holdings_normalised AS
SELECT
    h.holding_id,
    h.filing_id,
    h.issuer_id,
    -- 1. Normalized shares: scales by 100 if filer reported option contracts (30% tolerance band)
    CASE
        WHEN h.put_or_call IN (2, 3, 4, 5) -- PUT or CALL
         AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1) -- SH (not PRN)
         AND rp.sample_size >= 3
         AND ABS((
             (CASE WHEN f.filing_date < '2023-01-03'::date THEN h.value * 1000 ELSE h.value END)::numeric 
             / NULLIF(h.shares_or_principal_amount, 0)
         ) - (100 * rp.median_price)) <= (0.30 * (100 * rp.median_price))
        THEN (h.shares_or_principal_amount::bigint * 100)
        ELSE h.shares_or_principal_amount
    END AS shares_or_principal_amount,
    h.shares_or_principal_type,
    -- 2. Normalized value: pre-2023 filings in $1,000s scaled by 1,000, and auto-heals post-2023 filers in thousands
    CASE
        -- Pre-2023 filing already reported in full dollars (implied price ≈ 1000x median): keep raw value
        WHEN f.filing_date < '2023-01-03'::date AND rp.sample_size >= 3 
         AND ABS((h.value::numeric / NULLIF(h.shares_or_principal_amount, 0)) - (1000 * rp.median_price)) <= (0.25 * (1000 * rp.median_price))
        THEN h.value
        -- Standard pre-2023 filing (reported in $1,000s): scale by 1000
        WHEN f.filing_date < '2023-01-03'::date 
        THEN h.value * 1000
        -- Post-2023 filing mistakenly reported in $1,000s (implied price ≈ median / 1000): scale by 1000
        WHEN f.filing_date >= '2023-01-03'::date AND rp.sample_size >= 3
         AND ABS((h.value::numeric / NULLIF(h.shares_or_principal_amount, 0)) - (rp.median_price / 1000.0)) <= (0.25 * (rp.median_price / 1000.0))
        THEN h.value * 1000
        -- Standard post-2023 filing (already in full dollars): keep raw value
        ELSE h.value
    END AS value,
    h.title_of_class,
    h.put_or_call,
    h.investment_discretion,
    h.voting_authority_sole,
    h.voting_authority_shared,
    h.voting_authority_none,
    -- 3. Audit columns for transparency and verification
    h.shares_or_principal_amount AS shares_raw,
    CASE
        WHEN h.put_or_call IN (2, 3, 4, 5)
         AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1)
         AND rp.sample_size >= 3
         AND ABS((
             (CASE WHEN f.filing_date < '2023-01-03'::date THEN h.value * 1000 ELSE h.value END)::numeric 
             / NULLIF(h.shares_or_principal_amount, 0)
         ) - (100 * rp.median_price)) <= (0.30 * (100 * rp.median_price))
        THEN true
        ELSE false
    END AS shares_adjusted,
    h.value AS value_raw,
    CASE
        WHEN f.filing_date < '2023-01-03'::date AND rp.sample_size >= 3 
         AND ABS((h.value::numeric / NULLIF(h.shares_or_principal_amount, 0)) - (1000 * rp.median_price)) <= (0.25 * (1000 * rp.median_price))
        THEN false
        WHEN f.filing_date < '2023-01-03'::date 
        THEN true
        WHEN f.filing_date >= '2023-01-03'::date AND rp.sample_size >= 3
         AND ABS((h.value::numeric / NULLIF(h.shares_or_principal_amount, 0)) - (rp.median_price / 1000.0)) <= (0.25 * (rp.median_price / 1000.0))
        THEN true
        ELSE false
    END AS value_adjusted
FROM holdings h
JOIN filings f ON h.filing_id = f.filing_id
LEFT JOIN reference_prices rp 
    ON h.issuer_id = rp.issuer_id 
   AND f.period_of_report = rp.period_of_report;
