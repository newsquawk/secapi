#!/usr/bin/env python3
"""
scripts/characterize_outputs.py

Characterization and Regression Testing Harness for 13F Holdings Queries.
Captures baseline output across pre-2023, post-2023 penny stock, post-2023 standard,
and options filings, then diffs and verifies new query outputs.
"""

import argparse
import datetime
import decimal
import json
import os
import sys
from typing import Any, Dict, List

import psycopg2
from psycopg2.extras import RealDictCursor

# Add project root to sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from main import (
    COMMON_STOCK_TITLE_OF_CLASS,
    LATEST_ACTIVITY_QUERY_OPTIONS,
    LATEST_ACTIVITY_QUERY_V3,
    MODIFIED_OPTIMISED_STORIES_QUERY,
)

SNAPSHOT_PATH = os.path.join(PROJECT_ROOT, "tests", "snapshots", "baseline_current.json")

# 4 Test Cohorts identified from the local PostgreSQL database:
COHORTS = [
    {
        "name": "Cohort A (Pre-2023 Standard)",
        "description": "Pre-2023 filing reported in thousands of dollars ($1,000s)",
        "filing_id": 90765,
        "previous_filing_id": 90766,
        "cik": "1806820",
        "has_options": False,
    },
    {
        "name": "Cohort B (Post-2023 Penny Stocks)",
        "description": "Post-2023 filing containing securities with price < $1.00",
        "filing_id": 30980,
        "previous_filing_id": 30981,
        "cik": "1745981",
        "has_options": False,
    },
    {
        "name": "Cohort C (Post-2023 Standard)",
        "description": "Post-2023 standard filing (> $1.00 stocks) - should remain 100% stable",
        "filing_id": 319178,
        "previous_filing_id": 310506,
        "cik": "883965",
        "has_options": False,
    },
    {
        "name": "Cohort D (Options)",
        "description": "Filing containing Puts and Calls",
        "filing_id": 30431,
        "previous_filing_id": 30432,
        "cik": "1567755",
        "has_options": True,
    },
]


def json_serializer(obj: Any) -> Any:
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    raise TypeError(f"Type {type(obj)} not serializable")


def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "sec"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "password"),
    )


def fetch_filing_metadata(cur, filing_id: int, prev_filing_id: int) -> Dict[str, Any]:
    query = """
        SELECT
            f.filing_id,
            f.company_id,
            f.accession_number,
            f.period_of_report,
            f.filing_date,
            f.created_at,
            f.form_type,
            c.company_name,
            c.cik_number,
            c.aum,
            prev.filing_id AS previous_filing_id,
            prev.accession_number AS previous_accession_number
        FROM filings f
        JOIN companies c ON f.company_id = c.company_id
        LEFT JOIN filings prev ON prev.filing_id = %s
        WHERE f.filing_id = %s;
    """
    cur.execute(query, (prev_filing_id, filing_id))
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Filing {filing_id} not found in database")
    return dict(row)


def run_stories_query(cur, filing_meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    tuple_data = (
        filing_meta["filing_id"],
        filing_meta["company_id"],
        filing_meta["accession_number"],
        filing_meta["period_of_report"],
        filing_meta["filing_date"],
        filing_meta["created_at"],
        filing_meta["company_name"],
        filing_meta["cik_number"],
        filing_meta["aum"],
        filing_meta["previous_filing_id"],
        filing_meta["previous_accession_number"],
    )
    values_string = cur.mogrify("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", tuple_data).decode("utf-8")
    query = MODIFIED_OPTIMISED_STORIES_QUERY.replace("%s", values_string)
    filing_ids_to_process = [filing_meta["filing_id"]]
    if filing_meta["previous_filing_id"]:
        filing_ids_to_process.append(filing_meta["previous_filing_id"])

    params = {
        "valid_filing_ids": [filing_meta["filing_id"]],
        "filing_ids_to_process": filing_ids_to_process,
        "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
    }
    cur.execute(query, params)
    return [dict(r) for r in cur.fetchall()]


def run_activity_query(cur, filing_meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    tuple_data = (
        filing_meta["filing_id"],
        filing_meta["company_id"],
        filing_meta["accession_number"],
        filing_meta["period_of_report"],
        filing_meta["filing_date"],
        filing_meta["created_at"],
        filing_meta["company_name"],
        filing_meta["cik_number"],
        filing_meta["aum"],
        filing_meta["previous_filing_id"],
        filing_meta["previous_accession_number"],
    )
    values_string = cur.mogrify("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", tuple_data).decode("utf-8")
    query = LATEST_ACTIVITY_QUERY_V3.replace("%s", values_string)
    filing_ids_to_process = [filing_meta["filing_id"]]
    if filing_meta["previous_filing_id"]:
        filing_ids_to_process.append(filing_meta["previous_filing_id"])

    params = {
        "valid_filing_ids": [filing_meta["filing_id"]],
        "filing_ids_to_process": filing_ids_to_process,
        "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
    }
    cur.execute(query, params)
    return [dict(r) for r in cur.fetchall()]


def run_options_query(cur, filing_meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    tuple_data = (
        filing_meta["filing_id"],
        filing_meta["company_id"],
        filing_meta["accession_number"],
        filing_meta["period_of_report"],
        filing_meta["filing_date"],
        filing_meta["created_at"],
        filing_meta["company_name"],
        filing_meta["cik_number"],
        filing_meta["aum"],
        filing_meta["previous_filing_id"],
        filing_meta["previous_accession_number"],
    )
    values_string = cur.mogrify("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", tuple_data).decode("utf-8")
    query_template = LATEST_ACTIVITY_QUERY_OPTIONS.format(
        cusip_filter="",
        min_value_filter="",
    ).replace("%s", values_string)
    filing_ids_to_process = [filing_meta["filing_id"]]
    if filing_meta["previous_filing_id"]:
        filing_ids_to_process.append(filing_meta["previous_filing_id"])

    params = {
        "valid_filing_ids": [filing_meta["filing_id"]],
        "filing_ids_to_process": filing_ids_to_process,
        "put_or_call_patterns": ["%Put%", "%Call%"],
        "allowed_change_types": ["new", "closed", "increased", "decreased"],
    }
    cur.execute(query_template, params)
    return [dict(r) for r in cur.fetchall()]


def capture_baseline(output_path: str = SNAPSHOT_PATH):
    print(f"Connecting to database to capture baseline snapshots...")
    conn = get_db()
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    snapshot: Dict[str, Any] = {
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "cohorts": {},
    }

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for cohort in COHORTS:
            name = cohort["name"]
            print(f"  Processing {name} (filing {cohort['filing_id']})...")
            meta = fetch_filing_metadata(cur, cohort["filing_id"], cohort["previous_filing_id"])

            stories = run_stories_query(cur, meta)
            activity = run_activity_query(cur, meta)
            options = run_options_query(cur, meta) if cohort["has_options"] else []

            snapshot["cohorts"][name] = {
                "metadata": meta,
                "stories_count": len(stories),
                "stories": stories,
                "activity_count": len(activity),
                "activity": activity,
                "options_count": len(options),
                "options": options,
            }

    with open(output_path, "w") as f:
        json.dump(snapshot, f, indent=2, default=json_serializer)

    print(f" Baseline snapshot successfully saved to {output_path}")


def verify_diff(baseline_path: str = SNAPSHOT_PATH) -> bool:
    if not os.path.exists(baseline_path):
        print(f" Baseline snapshot file not found at {baseline_path}. Run --capture-baseline first.")
        return False

    with open(baseline_path, "r") as f:
        baseline = json.load(f)

    print(f"Connecting to database to compare against baseline ({baseline['captured_at']})...")
    conn = get_db()
    all_passed = True

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for cohort in COHORTS:
            name = cohort["name"]
            base = baseline["cohorts"].get(name)
            if not base:
                print(f"  [SKIP] Cohort {name} not found in baseline")
                continue

            print(f"\n=======================================================")
            print(f"Evaluating: {name}")
            print(f"=======================================================")
            meta = fetch_filing_metadata(cur, cohort["filing_id"], cohort["previous_filing_id"])
            current_activity = run_activity_query(cur, meta)
            base_activity = base["activity"]

            print(f"  Baseline rows: {len(base_activity)} | Current rows: {len(current_activity)}")
            if len(base_activity) != len(current_activity):
                print(f"  [FAIL] Row count mismatch! Baseline: {len(base_activity)}, Current: {len(current_activity)}")
                all_passed = False
                continue

            # Index baseline by CUSIP
            base_by_cusip = {r["cusip"]: r for r in base_activity}

            identical_count = 0
            corrected_count = 0
            inconsistency_count = 0

            for curr_row in current_activity:
                cusip = curr_row["cusip"]
                b_row = base_by_cusip.get(cusip)
                if not b_row:
                    print(f"  [WARN] New cusip {cusip} not in baseline")
                    continue

                curr_val = curr_row.get("current_value")
                base_val = b_row.get("current_value")
                curr_shares = curr_row.get("current_shares") or 0
                curr_price = curr_row.get("current_price_per_share") or 0

                # Check internal price/value consistency
                if curr_shares > 0 and curr_val is not None:
                    implied_price = round(float(curr_val) / float(curr_shares), 2)
                    if abs(implied_price - float(curr_price)) > 0.05:
                        inconsistency_count += 1

                # Cohort specific expectations
                raw_curr_shares = curr_row.get("current_shares")
                raw_base_shares = b_row.get("current_shares")
                if "Cohort C" in name:
                    # Standard post-2023 stocks MUST be 100% identical in value and shares
                    if curr_val == base_val and raw_curr_shares == raw_base_shares:
                        identical_count += 1
                    else:
                        print(f"  [FAIL] Unexpected change in standard stock {cusip}: base_val={base_val}, curr_val={curr_val}, base_shares={raw_base_shares}, curr_shares={raw_curr_shares}")
                        all_passed = False
                elif "Cohort B" in name:
                    # Penny stocks: if baseline tripped < 1.0 heuristic, curr_val should be corrected
                    if curr_val == base_val:
                        identical_count += 1
                    else:
                        corrected_count += 1
                elif "Cohort A" in name:
                    # Pre-2023: current_value should now be 1000x of base_val if base_val was in thousands
                    if base_val is not None and curr_val is not None:
                        if float(curr_val) == float(base_val) * 1000.0:
                            corrected_count += 1
                        elif float(curr_val) == float(base_val):
                            identical_count += 1
                elif "Cohort D" in name:
                    if curr_val == base_val and raw_curr_shares == raw_base_shares:
                        identical_count += 1
                    else:
                        corrected_count += 1

            # If this cohort has options, also verify options query output
            if cohort.get("has_options"):
                current_options = run_options_query(cur, meta)
                base_options = base.get("options", [])
                print(f"  Options rows:     {len(current_options)} (baseline: {len(base_options)})")
                if len(current_options) == len(base_options):
                    print(f"   Options query verification: PASS")
                else:
                    print(f"  [FAIL] Options count mismatch: base={len(base_options)}, curr={len(current_options)}")
                    all_passed = False

            print(f"  Identical rows:   {identical_count}")
            if corrected_count > 0:
                print(f"  Corrected rows:   {corrected_count} (as expected for normalization)")
            if inconsistency_count > 0:
                print(f"  [FAIL] Price/Value inconsistencies found: {inconsistency_count}")
                all_passed = False
            else:
                print(f"   Internal Price/Value consistency: 100% PASS (value / shares == price)")

            # Check new downstream fields if present in schema
            sample = current_activity[0] if current_activity else {}
            has_weight = "weight_pct" in sample
            has_value_pct = "value_pct" in sample
            has_form_type = "form_type" in sample
            print(f"  Downstream fields: weight_pct={has_weight}, value_pct={has_value_pct}, form_type={has_form_type}")

    if all_passed:
        print("\n ALL CHARACTERIZATION TESTS PASSED!")
    else:
        print("\n [!] CHARACTERIZATION TEST FAILURES DETECTED")
    return all_passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="13F Characterization Test Harness")
    parser.add_argument("--capture-baseline", action="store_true", help="Capture baseline snapshot")
    parser.add_argument("--verify-diff", action="store_true", help="Compare current outputs to baseline snapshot")
    parser.add_argument("--output", default=SNAPSHOT_PATH, help="Path for snapshot file")
    args = parser.parse_args()

    if args.capture_baseline:
        capture_baseline(args.output)
    elif args.verify_diff:
        success = verify_diff(args.output)
        sys.exit(0 if success else 1)
    else:
        parser.print_help()
