-- ============================================================================
-- Migration: 002_common_stock_flag.sql
-- Purpose: Add precomputed is_common_stock boolean flag and trigger to
--          title_of_class_table to eliminate runtime SQL regex matching.
-- Execution Time: < 1 second (title_of_class_table is a small dictionary table)
-- ============================================================================

-- 1. Add boolean column (defaults to FALSE)
ALTER TABLE title_of_class_table 
    ADD COLUMN IF NOT EXISTS is_common_stock BOOLEAN DEFAULT FALSE;

-- 2. Precompute is_common_stock for all existing rows
UPDATE title_of_class_table 
SET is_common_stock = (name ~* 'COM|CL A|COMMON STOCK|STOCK|COM SHS|CAP STK CL');

-- 3. Add partial index for fast common stock filtering
CREATE INDEX IF NOT EXISTS idx_title_of_class_common 
    ON title_of_class_table (id) 
    WHERE is_common_stock = TRUE;

-- 4. Trigger function for any future inserts/updates from autoingest workers
CREATE OR REPLACE FUNCTION set_is_common_stock()
RETURNS TRIGGER AS $$
BEGIN
    NEW.is_common_stock := (NEW.name ~* 'COM|CL A|COMMON STOCK|STOCK|COM SHS|CAP STK CL');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_set_is_common_stock ON title_of_class_table;
CREATE TRIGGER trg_set_is_common_stock
BEFORE INSERT OR UPDATE OF name ON title_of_class_table
FOR EACH ROW
EXECUTE FUNCTION set_is_common_stock();
