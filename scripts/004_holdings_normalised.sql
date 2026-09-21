-- ============================================================================
-- Migration: 004_holdings_normalised.sql
-- Purpose: Normalise 13F holding values across pre-2023 and post-2023 filings.
--
-- SEC Rule Background:
-- Prior to January 3, 2023, SEC Form 13F Special Instruction 8 mandated that
-- values be reported in thousands of dollars ($1,000s).
-- Effective January 3, 2023, the SEC updated the EDGAR Form 13F XML technical
-- specification to mandate whole dollars for all filings submitted on or after
-- that date (including Q4 2022 filings submitted in Jan/Feb 2023).
--
-- Therefore, the correct regulatory boundary is filing_date < '2023-01-03'.
-- Filings submitted to EDGAR before Jan 3, 2023 are in $1,000s (multiplied by 1000).
-- Filings submitted to EDGAR on/after Jan 3, 2023 are in whole dollars (kept as-is).
-- ============================================================================

CREATE OR REPLACE VIEW holdings_normalised AS
SELECT
    h.holding_id,
    h.filing_id,
    h.issuer_id,
    h.shares_or_principal_amount,
    h.shares_or_principal_type,
    CASE
        -- Filings received by EDGAR before January 3, 2023 were legally in $1,000s
        WHEN f.filing_date < '2023-01-03' THEN h.value * 1000
        -- Filings received by EDGAR on or after January 3, 2023 are legally in whole dollars
        ELSE h.value
    END AS value,
    h.title_of_class,
    h.put_or_call,
    h.investment_discretion,
    h.voting_authority_sole,
    h.voting_authority_shared,
    h.voting_authority_none
FROM holdings h
JOIN filings f ON h.filing_id = f.filing_id;

-- Refresh statistics on filings
ANALYZE filings;
