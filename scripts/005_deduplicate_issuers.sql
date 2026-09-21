-- ============================================================================
-- Migration: 005_deduplicate_issuers.sql
-- Purpose: Deduplicate case-variant CUSIP records in `issuers`, repoint `holdings`,
--          and enforce uppercase CUSIP integrity.
--
-- Background:
-- SEC filers submit Form 13F XML filings with inconsistent casing for CUSIPs
-- (e.g. '17275R102' vs '17275r102'). Because the historical `issuers` unique
-- index was case-sensitive, thousands of duplicate `issuer_id`s were created.
-- This caused portfolio deltas to fail to match between quarters, generating
-- severe phantom trades.
--
-- Note on Execution:
-- Because the `holdings` table contains ~90M rows, running a single unbatched
-- UPDATE would hold row locks and generate high WAL volume.
-- The recommended production migration runner is the companion batched Python script:
--
--     docker exec -it secapi_container python scripts/deduplicate_issuers.py --batch-size 100
--
-- Below are the reference schema constraints and index definitions enforced by
-- the migration.
-- ============================================================================

-- 1. Ensure all solitary lowercase CUSIPs are uppercased
UPDATE issuers 
SET cusip = UPPER(cusip) 
WHERE cusip != UPPER(cusip);

-- 2. Add database constraint ensuring no lowercase CUSIP can ever be inserted
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_issuers_cusip_upper'
    ) THEN
        ALTER TABLE issuers ADD CONSTRAINT chk_issuers_cusip_upper 
            CHECK (cusip IS NULL OR cusip = UPPER(cusip));
    END IF;
END $$;

-- 3. Add functional unique index on UPPER(cusip) concurrently
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_issuers_cusip_upper 
    ON issuers (UPPER(cusip));
