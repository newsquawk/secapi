-- ============================================================================
-- Migration: 001_performance_indexes.sql
-- Purpose: Add non-blocking composite & GIN indexes for high-frequency queries
-- Note: CONCURRENTLY is used to prevent table locks in production.
--       Must be run outside a transaction block (autocommit mode).
-- ============================================================================

-- 1. Enable pg_trgm extension for substring/autocomplete search
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 2. Companies table indexes
-- Accelerate CIK lookups (/managers/{cik}, /company/{cik}/compare/latest)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_companies_cik 
    ON companies (cik_number);

-- Accelerate AUM sorting and filtering (/filings/?sort_by=aum, /api/v1/search/companies_by_aum)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_companies_aum 
    ON companies (aum DESC NULLS LAST);

-- Accelerate autocomplete substring searches (/api/search/companies)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_companies_name_trgm 
    ON companies USING gin (company_name gin_trgm_ops);


-- 3. Filings table indexes
-- Accelerate finding predecessor filings per company (/stories, /activity, /flow)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_filings_company_period 
    ON filings (company_id, period_of_report DESC, filing_date DESC);

-- Accelerate candidate 13F filing discovery sorted by date
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_filings_date_form 
    ON filings (filing_date DESC, created_at DESC) 
    WHERE form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A');

-- Ensure accession_number index exists for fast lookup
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_filings_accession 
    ON filings (accession_number);


-- 4. Holdings table indexes
-- Accelerate fetching holdings by filing (/holdings/{accession_number}, /analysis)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_holdings_filing_id 
    ON holdings (filing_id);

-- Accelerate stock flow queries (/api/v1/flow/daily, /flow/aggregate)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_holdings_issuer_filing 
    ON holdings (issuer_id, filing_id);

-- Accelerate holdings filtering by title of class
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_holdings_filing_title 
    ON holdings (filing_id, title_of_class);


-- 5. Issuers table indexes
-- Accelerate identifier resolution (CUSIP <-> Symbol/Ticker)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_issuers_cusip 
    ON issuers (cusip);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_issuers_symbol 
    ON issuers (symbol);
