-- ============================================================================
-- Migration: 003_covering_flow_index.sql
-- Purpose: Add covering composite index for stock flow queries with shares included
-- Note: CONCURRENTLY is used to prevent table locks in production.
--       Must be run outside a transaction block (autocommit mode).
-- ============================================================================

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_holdings_issuer_filing_shares 
    ON holdings (issuer_id, filing_id) 
    INCLUDE (shares_or_principal_amount);
