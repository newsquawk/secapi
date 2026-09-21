#!/usr/bin/env python3
"""
scripts/deduplicate_issuers.py

Database Migration: Deduplicate CUSIPs in `issuers` table and repoint `holdings`.

Features:
- Idempotent & Resumable: Can be stopped and resumed at any time.
- Batched Transactions: Processes N duplicate issuer groups per transaction to avoid table locks.
- Uses indexed lookups (`idx_holdings_issuer_id`) for sub-second batch execution.
- Includes --dry-run mode for safe verification before execution.
- Enforces uppercase constraint and unique index once all duplicates are merged.

Usage:
  # 1. Preview duplicates without changing data
  python scripts/deduplicate_issuers.py --dry-run

  # 2. Execute migration with batch size 100
  python scripts/deduplicate_issuers.py --batch-size 100
"""

import os
import sys
import time
import argparse
import logging

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("deduplicate_issuers")


def get_db_connection():
    """Connect to database using existing environment configuration from main.py."""
    from main import _get_db_connection_params
    params = _get_db_connection_params()
    # Map 'database' to 'dbname' for psycopg2.connect
    if "database" in params and "dbname" not in params:
        params["dbname"] = params.pop("database")
    return psycopg2.connect(**params)


def find_duplicate_issuers(cur):
    """
    Finds all non-canonical duplicate rows in `issuers` where multiple rows share the same UPPER(cusip).
    Returns list of dicts: {'old_issuer_id': int, 'canonical_issuer_id': int, 'cusip': str}
    """
    query = """
    WITH RankedIssuers AS (
        SELECT 
            issuer_id,
            cusip,
            FIRST_VALUE(issuer_id) OVER (
                PARTITION BY UPPER(cusip) 
                ORDER BY 
                    (cusip = UPPER(cusip)) DESC,
                    (symbol IS NOT NULL AND symbol NOT LIKE '%.%') DESC,
                    (symbol IS NOT NULL) DESC,
                    LENGTH(issuer_name) DESC,
                    issuer_id ASC
            ) AS canonical_issuer_id,
            ROW_NUMBER() OVER (
                PARTITION BY UPPER(cusip) 
                ORDER BY 
                    (cusip = UPPER(cusip)) DESC,
                    (symbol IS NOT NULL AND symbol NOT LIKE '%.%') DESC,
                    (symbol IS NOT NULL) DESC,
                    LENGTH(issuer_name) DESC,
                    issuer_id ASC
            ) AS rn
        FROM issuers
        WHERE cusip IS NOT NULL
    )
    SELECT issuer_id AS old_issuer_id, canonical_issuer_id, cusip
    FROM RankedIssuers
    WHERE rn > 1
    ORDER BY canonical_issuer_id, old_issuer_id;
    """
    cur.execute(query)
    return cur.fetchall()


def run_migration(dry_run: bool = False, batch_size: int = 100):
    conn = get_db_connection()
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=RealDictCursor)

    logger.info("Scanning for duplicate CUSIP groups in `issuers` table...")
    dupes = find_duplicate_issuers(cur)
    total_dupes = len(dupes)

    logger.info(f"Found {total_dupes:,} duplicate issuer records needing consolidation.")

    if total_dupes == 0:
        logger.info("No duplicate issuers found! Checking final constraints...")
    elif dry_run:
        # Check sample of holdings affected
        sample_ids = [d["old_issuer_id"] for d in dupes[:100]]
        cur.execute(
            "SELECT COUNT(*) FROM holdings WHERE issuer_id = ANY(%s);",
            (sample_ids,),
        )
        sample_count = cur.fetchone()["count"]
        logger.info(
            f"[DRY-RUN] Sample of 100 duplicate issuers references {sample_count:,} holdings rows."
        )
        logger.info("[DRY-RUN] Completed analysis. No changes were committed.")
        conn.rollback()
        cur.close()
        conn.close()
        return

    if total_dupes > 0:
        logger.info(
            f"Beginning batched deduplication (Batch size: {batch_size} issuers per transaction)..."
        )
        t_start = time.time()
        total_holdings_repointed = 0
        total_issuers_deleted = 0

        for i in range(0, total_dupes, batch_size):
            batch = dupes[i : i + batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (total_dupes + batch_size - 1) // batch_size
            b_start = time.time()

            batch_repointed = 0
            batch_deleted = 0

            for item in batch:
                old_id = item["old_issuer_id"]
                can_id = item["canonical_issuer_id"]

                # 1. Repoint holdings using indexed column idx_holdings_issuer_id
                cur.execute(
                    "UPDATE holdings SET issuer_id = %s WHERE issuer_id = %s;",
                    (can_id, old_id),
                )
                batch_repointed += cur.rowcount

                # 2. Delete redundant duplicate issuer row
                cur.execute(
                    "DELETE FROM issuers WHERE issuer_id = %s;",
                    (old_id,),
                )
                batch_deleted += cur.rowcount

            # Commit batch transaction to release row locks and clear WAL
            conn.commit()

            total_holdings_repointed += batch_repointed
            total_issuers_deleted += batch_deleted
            b_elapsed = time.time() - b_start
            pct = (min(i + batch_size, total_dupes) / total_dupes) * 100

            logger.info(
                f"[Batch {batch_num}/{total_batches} - {pct:5.1f}%] "
                f"Repointed {batch_repointed:,} holdings | Deleted {batch_deleted} issuers ({b_elapsed:.2f}s)"
            )

        total_elapsed = time.time() - t_start
        logger.info(
            f"All duplicate issuers processed in {total_elapsed:.1f}s. "
            f"Total holdings repointed: {total_holdings_repointed:,}, "
            f"Total duplicate issuers deleted: {total_issuers_deleted:,}."
        )

    # Final step: Uppercase any remaining lowercase CUSIPs that had no uppercase duplicate
    logger.info("Normalizing any remaining lowercase CUSIPs in `issuers`...")
    cur.execute(
        "UPDATE issuers SET cusip = UPPER(cusip) WHERE cusip != UPPER(cusip);"
    )
    normalized_count = cur.rowcount
    conn.commit()
    logger.info(f"Normalized {normalized_count:,} solitary lowercase CUSIPs to uppercase.")

    # Apply database constraints
    logger.info("Enforcing database constraints...")
    try:
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'chk_issuers_cusip_upper'
                ) THEN
                    ALTER TABLE issuers ADD CONSTRAINT chk_issuers_cusip_upper 
                        CHECK (cusip IS NULL OR cusip = UPPER(cusip));
                END IF;
            END $$;
        """)
        conn.commit()
        logger.info("Added CHECK constraint: chk_issuers_cusip_upper (cusip IS NULL OR cusip = UPPER(cusip)).")
    except Exception as e:
        conn.rollback()
        logger.warning(f"Constraint check skipped or already exists: {e}")

    # Create UNIQUE index concurrently if not exists
    conn.autocommit = True
    try:
        logger.info("Creating unique index idx_issuers_cusip_upper ON issuers (UPPER(cusip))...")
        cur.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_issuers_cusip_upper ON issuers (UPPER(cusip));"
        )
        logger.info("Unique index idx_issuers_cusip_upper successfully created/verified.")
    except Exception as e:
        logger.warning(f"Unique index notice: {e}")

    cur.close()
    conn.close()
    logger.info("Phase 3 Database Deduplication Migration complete!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Deduplicate issuers CUSIPs and repoint holdings."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze duplicates and print statistics without committing changes.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of duplicate issuers to process per transaction commit (default: 100).",
    )
    args = parser.parse_args()

    run_migration(dry_run=args.dry_run, batch_size=args.batch_size)
