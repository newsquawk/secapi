-- ============================================================================
-- Migration: 008_stock_splits.sql
-- Purpose: Create `stock_splits` table to store detected or vendor-supplied
--          corporate action stock split factors.
--
-- Background:
-- Form 13F compares holdings across quarters. When a stock splits (e.g. 25:1
-- for Booking Holdings BKNG, or 5:1 for Carvana CVNA), raw quarter-over-quarter
-- share count diffs create astronomical false percentage increases (e.g. +77,824%)
-- and mislabel unchanged or trimmed positions as "Top Buyers".
--
-- Solution:
-- Store `split_factor` per CUSIP and reporting period (e.g. 25.0 for 25:1,
-- 0.3333 for 1:3 reverse split).
-- During quarter-over-quarter comparisons, rebase previous shares:
--     rebased_previous_shares = previous_shares * split_factor
-- ============================================================================

CREATE TABLE IF NOT EXISTS stock_splits (
    cusip VARCHAR(9) NOT NULL,
    period_of_report DATE NOT NULL,
    split_factor NUMERIC(8, 4) NOT NULL,
    holder_count INT NOT NULL DEFAULT 0,
    source VARCHAR(20) DEFAULT 'consensus',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (cusip, period_of_report)
);

ALTER TABLE stock_splits ADD COLUMN IF NOT EXISTS holder_count INT NOT NULL DEFAULT 0;
ALTER TABLE stock_splits ADD COLUMN IF NOT EXISTS source VARCHAR(20) DEFAULT 'consensus';

CREATE INDEX IF NOT EXISTS idx_stock_splits_period 
    ON stock_splits (period_of_report);
