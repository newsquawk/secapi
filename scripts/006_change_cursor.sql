-- ============================================================================
-- Migration: 006_change_cursor.sql
-- Purpose: Establish a monotonic change cursor on the `filings` table for the
--          Content Hub pull-based change feed (GET /changes, GET /stream).
--
-- What it does:
--   1. Makes `updated_at` DB-maintained: stamped with now() on EVERY insert and
--      update via a BEFORE trigger, so a revision / re-parse of an already-served
--      filing re-surfaces in the feed. The value is set by the DATABASE (not by
--      the external autoingest workers), so the cursor is monotonic against the
--      DB clock regardless of what — if anything — writers supply for the column.
--   2. Backfills historical rows so a cold (cursor-less) walk over
--      (updated_at, filing_id) follows roughly the original ingest order.
--   3. Emits a LISTEN/NOTIFY signal ('filing_changes') on every 13F insert or
--      revision so the SSE doorbell can wake consumers without polling MAX(id).
--   4. Adds the (updated_at, filing_id) keyset index the feed pages on.
--
-- Ordering matters:
--   - The backfill runs BEFORE the triggers exist, so it is neither overwritten
--     by the BEFORE trigger (which would stamp now() over every historical row,
--     destroying ingest order) nor does it emit one NOTIFY per historical row.
--   - The keyset index is built CONCURRENTLY, so it lives OUTSIDE the txn block.
--
-- Locking / runtime: the backfill row-locks every filing and SET NOT NULL scans
-- the table; on ~288k rows this is a few seconds. Run manually, off-peak. This
-- migration is NOT auto-applied at startup. It is idempotent — safe to re-run.
-- ============================================================================

BEGIN;

-- 1. Ensure the cursor column exists (already present in most environments;
--    IF NOT EXISTS makes this a cheap metadata no-op there).
ALTER TABLE filings
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

-- 2. Backfill NULLs ONLY — never clobber a real timestamp on re-run.
--    COALESCE order gives historical rows a stable, roughly ingest-ordered
--    value so a from-scratch content-hub crawl walks history in a sane order.
--    Ties (e.g. many filings sharing a filing_date) are fine: the composite
--    (updated_at, filing_id) cursor breaks them deterministically on filing_id.
UPDATE filings
   SET updated_at = COALESCE(
           updated_at,
           created_at::timestamptz,
           filing_date::timestamptz,
           now()
       )
 WHERE updated_at IS NULL;

-- 3. BEFORE trigger: the DB stamps updated_at on every write (authoritative).
--    Created AFTER the backfill so the backfill's values survive. Deliberately
--    UNCONDITIONAL — it overrides any client-supplied value so the cursor
--    reflects when *this* DB saw the write, and it runs for all form types so
--    the NOT NULL invariant below holds for every row, not just 13F.
CREATE OR REPLACE FUNCTION set_filing_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_set_filing_updated_at ON filings;
CREATE TRIGGER trg_set_filing_updated_at
BEFORE INSERT OR UPDATE ON filings
FOR EACH ROW
EXECUTE FUNCTION set_filing_updated_at();

-- 4. Every row now has a value and the trigger guarantees future ones — lock
--    the column down so the cursor can never encounter a NULL.
ALTER TABLE filings
    ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE filings
    ALTER COLUMN updated_at SET NOT NULL;

-- 5. AFTER trigger: wake the SSE doorbell on 13F inserts/revisions only, so the
--    feed's consumers are not nudged for filings the feed does not carry.
--    The payload is a tiny "data available" nudge (well under pg_notify's
--    8000-byte limit); consumers then pull GET /changes from their own cursor.
--    `ts` is formatted as the same ISO-8601 UTC string the change cursor uses,
--    so a replay of the last signal is directly usable as a head marker.
CREATE OR REPLACE FUNCTION notify_filing_change()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'filing_changes',
        json_build_object(
            'id',        NEW.accession_number,
            'filing_id', NEW.filing_id,
            'op',        CASE WHEN TG_OP = 'INSERT' THEN 'insert' ELSE 'revise' END,
            'ts',        to_char(NEW.updated_at AT TIME ZONE 'UTC',
                                 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
        )::text
    );
    RETURN NULL;  -- AFTER trigger: return value is ignored
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_notify_filing_change ON filings;
CREATE TRIGGER trg_notify_filing_change
AFTER INSERT OR UPDATE ON filings
FOR EACH ROW
WHEN (NEW.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A'))
EXECUTE FUNCTION notify_filing_change();

COMMIT;

-- 6. Keyset index for the change feed's (updated_at, filing_id) pagination.
--    CONCURRENTLY => MUST run OUTSIDE a transaction block (autocommit), matching
--    the convention in 001/003.
--    NOTE: if a prior CONCURRENTLY build was interrupted it leaves an INVALID
--          index that IF NOT EXISTS will skip; drop it first, then re-run:
--            DROP INDEX IF EXISTS idx_filings_change_cursor;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_filings_change_cursor
    ON filings (updated_at, filing_id);

-- 7. Refresh planner statistics for the new column + index.
ANALYZE filings;
