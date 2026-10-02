import datetime as dt
from typing import List, Optional
import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from config import (
    COMMON_STOCK_TITLE_OF_CLASS,
    CUSIP_TO_TICKER,
    RATE_LIMIT,
    limiter,
    logger,
)
from database import get_db_cursor, INTERNAL_ERROR_DETAIL
from sec_models import LatestActivityResponse, HoldingActivity, PaginationMetadata
from utils import resolve_identifiers

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic Models for Stories Feed
# ---------------------------------------------------------------------------
class SignificantHolding(BaseModel):
    """Represents a single significant holding for the story."""

    issuer_name: str
    cusip: Optional[str] = None
    ticker: Optional[str] = None
    shares_or_principal_amount: int
    value: int
    change_type: str
    price_per_share: Optional[float] = None


class HoldingChange(BaseModel):
    issuer_name: str
    cusip: Optional[str] = None
    ticker: Optional[str] = None
    shares_or_principal_amount: int
    change_in_share: int
    percent_change: Optional[float] = None
    price_per_share: Optional[float] = None
    price_per_unit: Optional[float] = None
    change_type: str


class StorySummary(BaseModel):
    """A summary of a single filing's story for a list view."""

    cik: str
    aum: Optional[int] = None
    latest_accession_number: str
    previous_accession_number: str
    company_name: str
    reporting_period: dt.date
    filing_date: dt.date
    top_new_position: Optional[SignificantHolding] = None
    top_closed_position: Optional[SignificantHolding] = None
    top_increased_position: Optional[HoldingChange] = None
    top_decreased_position: Optional[HoldingChange] = None
    top_new_other_securities: Optional[SignificantHolding] = None
    top_closed_other_securities: Optional[SignificantHolding] = None
    top_increased_other_securities: Optional[HoldingChange] = None
    top_decreased_other_securities: Optional[HoldingChange] = None


class LatestStoriesResponse(BaseModel):
    """The response for the latest stories list endpoint."""

    stories: List[StorySummary]
    has_next_page: bool = False
    pagination: Optional[PaginationMetadata] = None


# ---------------------------------------------------------------------------
# SQL Queries
# ---------------------------------------------------------------------------
def _count_tuple_columns(val_str: str) -> int:
    val_str = val_str.strip()
    if not val_str.startswith("("):
        return 12
    in_quote = False
    quote_char = None
    cols = 1
    i = 1
    n = len(val_str)
    while i < n:
        ch = val_str[i]
        if ch in ("'", '"'):
            if not in_quote:
                in_quote = True
                quote_char = ch
            elif ch == quote_char:
                if i + 1 < n and val_str[i + 1] == quote_char:
                    i += 1
                else:
                    in_quote = False
        elif not in_quote:
            if ch == ",":
                cols += 1
            elif ch == ")":
                break
        i += 1
    return cols


class SmartQuery(str):
    """
    SQL query template string helper that:
    1. Injects sensible defaults for query formatting keys (order_by_clause, cusip_filter, etc.)
    2. Seamlessly supports both 12-column production VALUES tuples (passing form_type directly)
       and 11-column legacy VALUES tuples (from characterization/test harnesses) without error.
    """

    def format(self, *args, **kwargs):
        defaults = {
            "candidate_filter": "",
            "candidate_limit": "LIMIT 10000",
            "latest_filter": """AND EXISTS (
          SELECT 1 FROM holdings h
          JOIN put_or_call_table p ON h.put_or_call = p.id
          {issuer_join}
          WHERE h.filing_id = rf.filing_id
            AND p.name ILIKE ANY(%(put_or_call_patterns)s)
            {cusip_filter}
      )""",
            "issuer_join": "",
            "cusip_filter": "",
            "min_value_filter": "",
            "order_by_clause": "hc.filing_date DESC, hc.created_at DESC",
            "form_type_col": "",
            "form_type_expr": "(SELECT form_type FROM filings WHERE filing_id = fwp.filing_id)",
        }
        merged = {**defaults, **kwargs}
        if (
            "latest_filter" in defaults
            and ("issuer_join" in kwargs or "cusip_filter" in kwargs)
            and "{issuer_join}" in merged["latest_filter"]
        ):
            merged["latest_filter"] = merged["latest_filter"].format(
                issuer_join=merged.get("issuer_join", ""),
                cusip_filter=merged.get("cusip_filter", ""),
            )
        res = super().format(*args, **merged)
        return SmartQuery(res)

    def replace(self, old, new, count=-1):
        txt = str(self)
        if "{" in txt and "}" in txt:
            txt = str(self.format())
        if old == "%s":
            cols = _count_tuple_columns(new)
            if cols == 11:
                txt = txt.replace(", form_type", "").replace(
                    "fwp.form_type AS form_type",
                    "(SELECT form_type FROM filings WHERE filing_id = fwp.filing_id) AS form_type",
                )
            elif cols == 12:
                if ", form_type" not in txt:
                    txt = txt.replace(
                        "previous_accession_number", "previous_accession_number, form_type"
                    )
                txt = txt.replace(
                    "(SELECT form_type FROM filings WHERE filing_id = fwp.filing_id) AS form_type",
                    "fwp.form_type AS form_type",
                )
        return txt.replace(old, new, count)


FILING_QUERY_V2 = """
WITH CandidateFilings AS (
    SELECT
        f.filing_id,
        f.company_id,
        f.accession_number,
        f.period_of_report,
        f.filing_date,
        f.created_at,
        f.form_type
    FROM
        filings f
    WHERE
        f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ORDER BY f.filing_date DESC, f.created_at DESC
    LIMIT 2000
),
RankedFilings AS (
    SELECT
        filing_id,
        company_id,
        accession_number,
        period_of_report,
        filing_date,
        created_at,
        form_type,
        ROW_NUMBER() OVER(PARTITION BY company_id ORDER BY filing_date DESC, created_at DESC) as rn
    FROM
        CandidateFilings
),
LatestFilings AS (
    SELECT
        filing_id,
        company_id,
        accession_number,
        period_of_report,
        filing_date,
        created_at,
        form_type
    FROM RankedFilings
    WHERE rn = 1
    ORDER BY filing_date DESC, created_at DESC
    LIMIT %(limit)s OFFSET %(offset)s
)
SELECT
    lf.filing_id,
    lf.company_id,
    lf.accession_number,
    lf.period_of_report,
    lf.filing_date,
    lf.created_at,
    lf.form_type,
    c.company_name,
    c.cik_number,
    c.aum,
    pf.filing_id AS previous_filing_id,
    pf.accession_number AS previous_accession_number
FROM LatestFilings lf
JOIN companies c ON lf.company_id = c.company_id
LEFT JOIN LATERAL (
    SELECT filing_id, accession_number
    FROM filings
    WHERE company_id = lf.company_id
        AND period_of_report < lf.period_of_report
        AND form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ORDER BY period_of_report DESC, filing_date DESC
    LIMIT 1
) pf ON true
ORDER BY lf.filing_date DESC, lf.created_at DESC;
"""

FILTER_CANDIDATES_QUERY_V2 = """
    SELECT DISTINCT h.filing_id
    FROM holdings h
    JOIN title_of_class_table tc ON h.title_of_class = tc.id
    WHERE
        h.filing_id = ANY(%(candidate_filing_ids)s)
      AND tc.is_common_stock = TRUE;
"""

MODIFIED_OPTIMISED_STORIES_QUERY = """
    WITH FilingsWithPrevious AS (
        SELECT * FROM (VALUES %s) AS t (
            filing_id, company_id, accession_number, period_of_report, filing_date,
            created_at,
            company_name, cik_number, aum, previous_filing_id, previous_accession_number
        )
        WHERE filing_id = ANY(%(valid_filing_ids)s)
    ),
    AggregatedHoldings AS (
        SELECT
            h.filing_id,
            i.cusip,
            MIN(i.issuer_name) as issuer_name,
            SUM(h.value) as total_value,
            SUM(h.shares_or_principal_amount) as total_shares,
            tc.is_common_stock
        FROM holdings_normalised h
        JOIN issuers i ON h.issuer_id = i.issuer_id
        JOIN title_of_class_table tc ON h.title_of_class = tc.id
        LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
        WHERE h.filing_id = ANY(%(filing_ids_to_process)s)
          AND (poc.name IS NULL OR upper(poc.name) NOT IN ('PUT', 'CALL'))
        GROUP BY h.filing_id, i.cusip, tc.is_common_stock
    ),
    HoldingsComparison AS (
        SELECT
            fwp.filing_id,
            hc.*
        FROM
            FilingsWithPrevious fwp
        LEFT JOIN LATERAL (
            SELECT
                COALESCE(curr.cusip, prev.cusip) AS cusip,
                COALESCE(curr.issuer_name, prev.issuer_name) AS issuer_name,
                curr.total_value AS current_value,
                curr.total_shares AS current_shares,
                prev.total_value AS previous_value,
                prev.total_shares AS previous_shares,
                (curr.total_shares - prev.total_shares) AS change_in_share,
                CASE
                    WHEN prev.total_shares > 0 THEN ((curr.total_shares - prev.total_shares)::numeric / prev.total_shares) * 100
                    ELSE NULL
                END AS percent_change,
                CASE
                    WHEN prev.cusip IS NULL THEN 'new'
                    WHEN curr.cusip IS NULL THEN 'closed'
                    WHEN curr.total_shares > prev.total_shares THEN 'increased'
                    WHEN curr.total_shares < prev.total_shares THEN 'decreased'
                    ELSE 'unchanged'
                END as change_type,
                COALESCE(curr.is_common_stock, prev.is_common_stock) AS is_common_stock
            FROM
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.filing_id) AS curr
            FULL OUTER JOIN
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.previous_filing_id) AS prev
            ON curr.cusip = prev.cusip AND curr.is_common_stock = prev.is_common_stock
            WHERE
                (curr.cusip IS NOT NULL OR prev.cusip IS NOT NULL)
        ) hc ON true
        WHERE fwp.previous_filing_id IS NOT NULL
    ),
    RankedChanges AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY filing_id, is_common_stock, change_type
                ORDER BY
                    CASE WHEN change_type IN ('new', 'closed') THEN COALESCE(current_value, previous_value) END DESC,
                    CASE WHEN change_type IN ('increased', 'decreased') THEN ABS(percent_change) END DESC
            ) as rn
        FROM HoldingsComparison
        WHERE change_type IN ('new', 'closed', 'increased', 'decreased')
    )
    SELECT
        fwp.cik_number AS cik,
        fwp.aum,
        fwp.accession_number AS latest_accession_number,
        fwp.previous_accession_number,
        fwp.company_name,
        fwp.period_of_report AS reporting_period,
        fwp.filing_date,
        -- Common Stock - New
        MAX(CASE WHEN rc.change_type = 'new' AND rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_new_issuer,
        MAX(CASE WHEN rc.change_type = 'new' AND rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_new_cusip,
        MAX(CASE WHEN rc.change_type = 'new' AND rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_new_shares,
        MAX(CASE WHEN rc.change_type = 'new' AND rc.is_common_stock AND rc.rn = 1 THEN rc.current_value END) AS top_new_value,
        MAX(CASE WHEN rc.change_type = 'new' AND rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            (rc.current_value::numeric) / rc.current_shares
        ELSE 0 END) AS top_new_price,
        -- Common Stock - Closed
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_closed_issuer,
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_closed_cusip,
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 THEN rc.previous_shares END) AS top_closed_shares,
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 THEN rc.previous_value END) AS top_closed_value,
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 AND rc.previous_shares > 0 THEN
            (rc.previous_value::numeric) / rc.previous_shares
        ELSE 0 END) AS top_closed_price,
        -- Common Stock - Increased
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_increased_issuer,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_increased_cusip,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_increased_shares,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.change_in_share END) AS top_increased_change_in_share,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.percent_change END) AS top_increased_percent_change,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            (rc.current_value::numeric) / rc.current_shares
        ELSE 0 END) AS top_increased_price,
        -- Common Stock - Decreased
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_decreased_issuer,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_decreased_cusip,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_decreased_shares,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.change_in_share END) AS top_decreased_change_in_share,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.percent_change END) AS top_decreased_percent_change,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            (rc.current_value::numeric) / rc.current_shares
        ELSE 0 END) AS top_decreased_price,
        -- Other Securities - New
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_new_other_issuer,
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_new_other_cusip,
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_new_other_shares,
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.current_value END) AS top_new_other_value,
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            (rc.current_value::numeric) / rc.current_shares
        ELSE 0 END) AS top_new_other_price,  
        -- Other Securities - Closed
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_closed_other_issuer,
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_closed_other_cusip,
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.previous_shares END) AS top_closed_other_shares,
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.previous_value END) AS top_closed_other_value,
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 AND rc.previous_shares > 0 THEN
            (rc.previous_value::numeric) / rc.previous_shares
        ELSE 0 END) AS top_closed_other_price,
        -- Other Securities - Increased
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_increased_other_issuer,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_increased_other_cusip,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_increased_other_shares,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.change_in_share END) AS top_increased_other_change_in_share,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.percent_change END) AS top_increased_other_percent_change,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            (rc.current_value::numeric) / rc.current_shares
        ELSE 0 END) AS top_increased_other_price,
        -- Other Securities - Decreased
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_decreased_other_issuer,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_decreased_other_cusip,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_decreased_other_shares,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.change_in_share END) AS top_decreased_other_change_in_share,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.percent_change END) AS top_decreased_other_percent_change,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            (rc.current_value::numeric) / rc.current_shares
        ELSE 0 END) AS top_decreased_other_price
    FROM
        FilingsWithPrevious fwp
    LEFT JOIN
        RankedChanges rc ON fwp.filing_id = rc.filing_id
    GROUP BY
        fwp.filing_id, fwp.cik_number, fwp.aum, fwp.accession_number, fwp.previous_accession_number,
        fwp.company_name, fwp.period_of_report, fwp.filing_date, fwp.created_at
    ORDER BY
        fwp.filing_date DESC, fwp.created_at DESC;
"""

LATEST_ACTIVITY_QUERY_V3 = SmartQuery("""
    WITH FilingsWithPrevious AS (
        SELECT * FROM (VALUES %s) AS t (
            filing_id, company_id, accession_number, period_of_report, filing_date,
            created_at,
            company_name, cik_number, aum, previous_filing_id, previous_accession_number{form_type_col}
        )
        WHERE filing_id = ANY(%(valid_filing_ids)s)
    ),
    AggregatedHoldings AS (
        SELECT
            h.filing_id,
            i.cusip,
            MIN(i.issuer_name) as issuer_name,
            SUM(h.value) as total_value,
            SUM(h.shares_or_principal_amount) as total_shares,
            tc.is_common_stock
        FROM holdings_normalised h
        JOIN issuers i ON h.issuer_id = i.issuer_id
        JOIN title_of_class_table tc ON h.title_of_class = tc.id
        LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
        WHERE h.filing_id = ANY(%(filing_ids_to_process)s)
          AND (poc.name IS NULL OR upper(poc.name) NOT IN ('PUT', 'CALL'))
        GROUP BY h.filing_id, i.cusip, tc.is_common_stock
    ),
    HoldingsComparison AS (
        SELECT
            fwp.filing_id,
            fwp.aum,
            fwp.cik_number,
            fwp.company_name,
            fwp.accession_number,
            fwp.previous_accession_number,
            fwp.period_of_report,
            fwp.filing_date,
            fwp.created_at,
            {form_type_expr} AS form_type,
            hc.cusip,
            hc.issuer_name,
            hc.current_value,
            hc.current_shares,
            hc.previous_value,
            hc.previous_shares,
            hc.change_in_share,
            hc.percent_change,
            hc.change_type,
            hc.is_common_stock
        FROM
            FilingsWithPrevious fwp
        LEFT JOIN LATERAL (
            SELECT
                COALESCE(curr.cusip, prev.cusip) AS cusip,
                COALESCE(curr.issuer_name, prev.issuer_name) AS issuer_name,
                curr.total_value AS current_value,
                curr.total_shares AS current_shares,
                prev.total_value AS previous_value,
                prev.total_shares AS previous_shares,
                (curr.total_shares - prev.total_shares) AS change_in_share,
                CASE
                    WHEN prev.total_shares > 0 THEN ((curr.total_shares - prev.total_shares)::numeric / prev.total_shares) * 100
                    ELSE NULL
                END AS percent_change,
                CASE
                    WHEN prev.cusip IS NULL THEN 'new'
                    WHEN curr.cusip IS NULL THEN 'closed'
                    WHEN curr.total_shares > prev.total_shares THEN 'increased'
                    WHEN curr.total_shares < prev.total_shares THEN 'decreased'
                    ELSE 'unchanged'
                END as change_type,
                COALESCE(curr.is_common_stock, prev.is_common_stock) AS is_common_stock
            FROM
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.filing_id) AS curr
            FULL OUTER JOIN
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.previous_filing_id) AS prev
                ON curr.cusip = prev.cusip AND curr.is_common_stock = prev.is_common_stock
            WHERE (curr.cusip IS NOT NULL OR prev.cusip IS NOT NULL)
        ) hc ON true
        WHERE
            fwp.previous_filing_id IS NOT NULL
    )
    SELECT
        hc.cik_number AS cik,
        hc.company_name,
        hc.accession_number AS latest_accession_number,
        hc.previous_accession_number,
        hc.period_of_report AS reporting_period,
        hc.filing_date,
        hc.form_type,
        hc.issuer_name,
        hc.cusip,
        hc.is_common_stock,
        hc.change_type,
        hc.current_shares,
        hc.previous_shares,
        hc.change_in_share,
        ROUND(hc.percent_change::numeric, 2) AS percent_change,
        hc.current_value,
        hc.previous_value,
        ABS(COALESCE(hc.current_value, 0) - COALESCE(hc.previous_value, 0)) AS absolute_value_change,
        hc.aum,
        ROUND(
            CASE
                WHEN hc.current_shares > 0 THEN (hc.current_value::numeric) / hc.current_shares
                ELSE 0
            END, 2
        ) AS current_price_per_share,
        ROUND(
            CASE
                WHEN hc.previous_shares > 0 THEN (hc.previous_value::numeric) / hc.previous_shares
                ELSE 0
            END, 2
        ) AS previous_price_per_share,
        ROUND(
            CASE
                WHEN hc.aum > 0 AND hc.current_value > 0 THEN
                    (hc.current_value::numeric / hc.aum) * 100
                ELSE NULL
            END, 4
        ) AS weight_pct,
        ROUND(
            CASE
                WHEN hc.previous_value > 0 AND hc.current_value IS NOT NULL THEN
                    ((hc.current_value::numeric - hc.previous_value::numeric) / hc.previous_value::numeric) * 100
                ELSE NULL
            END, 2
        ) AS value_pct
    FROM
        HoldingsComparison hc
    WHERE
        hc.change_type IN ('new', 'closed', 'increased', 'decreased')
        {min_value_filter}
    ORDER BY
        {order_by_clause};
""")

FILING_QUERY_OPTIONS = SmartQuery("""
WITH CandidateFilings AS (
    SELECT
        f.filing_id,
        f.company_id,
        f.accession_number,
        f.period_of_report,
        f.filing_date,
        f.created_at,
        f.form_type
    FROM
        filings f
    WHERE
        f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
        {candidate_filter}
    ORDER BY f.filing_date DESC, f.created_at DESC
    {candidate_limit}
),
RankedFilings AS (
    SELECT
        filing_id,
        company_id,
        accession_number,
        period_of_report,
        filing_date,
        created_at,
        form_type,
        ROW_NUMBER() OVER(PARTITION BY company_id ORDER BY filing_date DESC, created_at DESC) as rn
    FROM
        CandidateFilings
),
LatestFilings AS (
    SELECT
        rf.filing_id,
        rf.company_id,
        rf.accession_number,
        rf.period_of_report,
        rf.filing_date,
        rf.created_at,
        rf.form_type
    FROM RankedFilings rf
    WHERE rf.rn = 1
      {latest_filter}
    ORDER BY rf.filing_date DESC, rf.created_at DESC
    LIMIT %(limit)s OFFSET %(offset)s
)
SELECT
    lf.filing_id,
    lf.company_id,
    lf.accession_number,
    lf.period_of_report,
    lf.filing_date,
    lf.created_at,
    lf.form_type,
    c.company_name,
    c.cik_number,
    c.aum,
    pf.filing_id AS previous_filing_id,
    pf.accession_number AS previous_accession_number
FROM LatestFilings lf
JOIN companies c ON lf.company_id = c.company_id
LEFT JOIN LATERAL (
    SELECT filing_id, accession_number
    FROM filings
    WHERE company_id = lf.company_id
        AND period_of_report < lf.period_of_report
        AND form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ORDER BY period_of_report DESC, filing_date DESC
    LIMIT 1
) pf ON true
ORDER BY lf.filing_date DESC, lf.created_at DESC;
""")

LATEST_ACTIVITY_QUERY_OPTIONS = SmartQuery("""
    WITH FilingsWithPrevious AS (
        SELECT * FROM (VALUES %s) AS t (
            filing_id, company_id, accession_number, period_of_report, filing_date,
            created_at,
            company_name, cik_number, aum, previous_filing_id, previous_accession_number{form_type_col}
        )
        WHERE filing_id = ANY(%(valid_filing_ids)s)
    ),
    AggregatedHoldings AS (
        SELECT
            h.filing_id,
            UPPER(i.cusip) AS cusip,
            UPPER(p.name) AS put_or_call,
            MIN(i.issuer_name) as issuer_name,
            SUM(h.value) as total_value,
            SUM(h.shares_or_principal_amount) as total_shares
        FROM holdings_normalised h
        JOIN issuers i ON h.issuer_id = i.issuer_id
        JOIN put_or_call_table p ON h.put_or_call = p.id
        WHERE h.filing_id = ANY(%(filing_ids_to_process)s)
          AND p.name ILIKE ANY(%(put_or_call_patterns)s)
          {cusip_filter}
        GROUP BY h.filing_id, UPPER(i.cusip), UPPER(p.name)
    ),
    HoldingsComparison AS (
        SELECT
            fwp.filing_id,
            fwp.aum,
            fwp.cik_number,
            fwp.company_name,
            fwp.accession_number,
            fwp.previous_accession_number,
            fwp.period_of_report,
            fwp.filing_date,
            fwp.created_at,
            {form_type_expr} AS form_type,
            hc.cusip,
            hc.put_or_call,
            hc.issuer_name,
            hc.current_value,
            hc.current_shares,
            hc.previous_value,
            hc.previous_shares,
            hc.change_in_share,
            hc.percent_change,
            hc.change_type
        FROM
            FilingsWithPrevious fwp
        LEFT JOIN LATERAL (
            SELECT
                COALESCE(curr.cusip, prev.cusip) AS cusip,
                COALESCE(curr.put_or_call, prev.put_or_call) AS put_or_call,
                COALESCE(curr.issuer_name, prev.issuer_name) AS issuer_name,
                curr.total_value AS current_value,
                curr.total_shares AS current_shares,
                prev.total_value AS previous_value,
                prev.total_shares AS previous_shares,
                (curr.total_shares - prev.total_shares) AS change_in_share,
                CASE
                    WHEN prev.total_shares > 0 THEN ((curr.total_shares - prev.total_shares)::numeric / prev.total_shares) * 100
                    ELSE NULL
                END AS percent_change,
                CASE
                    WHEN prev.cusip IS NULL THEN 'new'
                    WHEN curr.cusip IS NULL THEN 'closed'
                    WHEN curr.total_shares > prev.total_shares THEN 'increased'
                    WHEN curr.total_shares < prev.total_shares THEN 'decreased'
                    ELSE 'unchanged'
                END as change_type
            FROM
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.filing_id) AS curr
            FULL OUTER JOIN
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.previous_filing_id) AS prev
                ON curr.cusip = prev.cusip AND curr.put_or_call = prev.put_or_call
            WHERE (curr.cusip IS NOT NULL OR prev.cusip IS NOT NULL)
        ) hc ON true
        WHERE
            fwp.previous_filing_id IS NOT NULL
    )
    SELECT
        hc.cik_number AS cik,
        hc.company_name,
        hc.accession_number AS latest_accession_number,
        hc.previous_accession_number,
        hc.period_of_report AS reporting_period,
        hc.filing_date,
        hc.form_type,
        hc.issuer_name,
        hc.cusip,
        hc.put_or_call,
        FALSE AS is_common_stock,
        hc.change_type,
        hc.current_shares,
        hc.previous_shares,
        hc.change_in_share,
        ROUND(hc.percent_change::numeric, 2) AS percent_change,
        hc.current_value,
        hc.previous_value,
        ABS(COALESCE(hc.current_value, 0) - COALESCE(hc.previous_value, 0)) AS absolute_value_change,
        hc.aum,
        ROUND(
            CASE
                WHEN hc.current_shares > 0 THEN (hc.current_value::numeric) / hc.current_shares
                ELSE 0
            END, 2
        ) AS current_price_per_share,
        ROUND(
            CASE
                WHEN hc.previous_shares > 0 THEN (hc.previous_value::numeric) / hc.previous_shares
                ELSE 0
            END, 2
        ) AS previous_price_per_share,
        ROUND(
            CASE
                WHEN hc.aum > 0 AND hc.current_value > 0 THEN
                    (hc.current_value::numeric / hc.aum) * 100
                ELSE NULL
            END, 4
        ) AS weight_pct,
        ROUND(
            CASE
                WHEN hc.previous_value > 0 AND hc.current_value IS NOT NULL THEN
                    ((hc.current_value::numeric - hc.previous_value::numeric) / hc.previous_value::numeric) * 100
                ELSE NULL
            END, 2
        ) AS value_pct
    FROM
        HoldingsComparison hc
    WHERE
        hc.change_type = ANY(%(allowed_change_types)s)
        {min_value_filter}
    ORDER BY
        {order_by_clause};
""")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/stories/latest/v2", response_model=LatestStoriesResponse)
@router.get("/stories/latest/v2/", response_model=LatestStoriesResponse, include_in_schema=False)
@limiter.limit(RATE_LIMIT)
def get_latest_stories_v2(
    request: Request,
    limit: int = Query(20, description="Number of stories to return", ge=1, le=50),
    offset: int = Query(0, description="Number of stories to skip for pagination", ge=0),
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieves a list of the latest filings, each with a summary of its most
    significant new holding.
    """
    try:
        logger.info(f"Fetching latest stories v2 with limit {limit} and offset {offset}")
        db.execute(FILING_QUERY_V2, {"limit": limit + 1, "offset": offset})
        candidate_filings = db.fetchall()

        has_next_page = len(candidate_filings) > limit
        candidates_to_process = candidate_filings[:limit]

        next_offset = offset + limit if has_next_page else None
        pagination = PaginationMetadata(
            limit=limit,
            offset=offset,
            total=None,
            has_more=has_next_page,
            next_offset=next_offset,
        )

        if response:
            response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=120"
            response.headers["X-Has-More"] = str(has_next_page).lower()
            if next_offset is not None:
                response.headers["X-Next-Offset"] = str(next_offset)
            response.headers["X-Limit"] = str(limit)
            response.headers["X-Offset"] = str(offset)

        if not candidates_to_process:
            logger.info("No candidate filings found for the given limit and offset.")
            return LatestStoriesResponse(
                stories=[],
                has_next_page=False,
                pagination=PaginationMetadata(
                    limit=limit,
                    offset=offset,
                    total=None,
                    has_more=False,
                    next_offset=None,
                ),
            )

        candidate_filing_ids = [f["filing_id"] for f in candidates_to_process]
        params = {
            "candidate_filing_ids": candidate_filing_ids,
            "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
        }
        logger.info("Filtering candidate filings")
        db.execute(FILTER_CANDIDATES_QUERY_V2, params)
        valid_filing_ids = {row["filing_id"] for row in db.fetchall()}

        valid_filings = [
            f for f in candidate_filings if f["filing_id"] in valid_filing_ids
        ]

        if not valid_filings:
            logger.info("No valid filings found after filtering.")
            return LatestStoriesResponse(
                stories=[],
                has_next_page=has_next_page,
                pagination=pagination,
            )

        filing_ids_to_process = set()
        filing_data_tuples = []

        for f in valid_filings:
            filing_ids_to_process.add(f["filing_id"])
            if f["previous_filing_id"]:
                filing_ids_to_process.add(f["previous_filing_id"])

            filing_data_tuples.append(
                (
                    f["filing_id"],
                    f["company_id"],
                    f["accession_number"],
                    f["period_of_report"],
                    f["filing_date"],
                    f["created_at"],
                    f["company_name"],
                    f["cik_number"],
                    f["aum"],
                    f["previous_filing_id"],
                    f["previous_accession_number"],
                )
            )

        values_string_list = []
        for t in filing_data_tuples:
            values_string_list.append(
                db.mogrify("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", t).decode("utf-8")
            )

        values_string = ",\n".join(values_string_list)
        final_query_template = MODIFIED_OPTIMISED_STORIES_QUERY.replace("%s", values_string)

        final_query_params = {
            "valid_filing_ids": list(valid_filing_ids),
            "filing_ids_to_process": list(filing_ids_to_process),
            "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
        }
        db.execute(final_query_template, final_query_params)
        results = db.fetchall()

        story_summaries = []
        for row in results:
            top_new = (
                SignificantHolding(
                    issuer_name=row["top_new_issuer"],
                    cusip=row["top_new_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_new_cusip"]),
                    shares_or_principal_amount=row["top_new_shares"],
                    value=row["top_new_value"],
                    price_per_share=row["top_new_price"],
                    change_type="new",
                )
                if row["top_new_issuer"]
                else None
            )

            top_closed = (
                SignificantHolding(
                    issuer_name=row["top_closed_issuer"],
                    cusip=row["top_closed_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_closed_cusip"]),
                    shares_or_principal_amount=row["top_closed_shares"],
                    value=row["top_closed_value"],
                    price_per_share=row["top_closed_price"],
                    change_type="closed",
                )
                if row["top_closed_issuer"]
                else None
            )

            top_increased = (
                HoldingChange(
                    issuer_name=row["top_increased_issuer"],
                    cusip=row["top_increased_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_increased_cusip"]),
                    shares_or_principal_amount=row["top_increased_shares"],
                    change_in_share=row["top_increased_change_in_share"],
                    percent_change=row["top_increased_percent_change"],
                    price_per_share=row["top_increased_price"],
                    change_type="increased",
                )
                if row["top_increased_issuer"]
                else None
            )

            top_decreased = (
                HoldingChange(
                    issuer_name=row["top_decreased_issuer"],
                    cusip=row["top_decreased_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_decreased_cusip"]),
                    shares_or_principal_amount=row["top_decreased_shares"],
                    change_in_share=row["top_decreased_change_in_share"],
                    percent_change=row["top_decreased_percent_change"],
                    price_per_share=row["top_decreased_price"],
                    change_type="decreased",
                )
                if row["top_decreased_issuer"]
                else None
            )

            top_new_other = (
                SignificantHolding(
                    issuer_name=row["top_new_other_issuer"],
                    cusip=row["top_new_other_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_new_other_cusip"]),
                    shares_or_principal_amount=row["top_new_other_shares"],
                    value=row["top_new_other_value"],
                    price_per_share=row["top_new_other_price"],
                    change_type="new",
                )
                if row.get("top_new_other_issuer")
                else None
            )

            top_closed_other = (
                SignificantHolding(
                    issuer_name=row["top_closed_other_issuer"],
                    cusip=row["top_closed_other_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_closed_other_cusip"]),
                    shares_or_principal_amount=row["top_closed_other_shares"],
                    value=row["top_closed_other_value"],
                    price_per_share=row["top_closed_other_price"],
                    change_type="closed",
                )
                if row.get("top_closed_other_issuer")
                else None
            )

            top_increased_other = (
                HoldingChange(
                    issuer_name=row["top_increased_other_issuer"],
                    cusip=row["top_increased_other_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_increased_other_cusip"]),
                    shares_or_principal_amount=row["top_increased_other_shares"],
                    change_in_share=row["top_increased_other_change_in_share"],
                    percent_change=row["top_increased_other_percent_change"],
                    price_per_unit=row["top_increased_other_price"],
                    change_type="increased",
                )
                if row.get("top_increased_other_issuer")
                else None
            )

            top_decreased_other = (
                HoldingChange(
                    issuer_name=row["top_decreased_other_issuer"],
                    cusip=row["top_decreased_other_cusip"],
                    ticker=CUSIP_TO_TICKER.get(row["top_decreased_other_cusip"]),
                    shares_or_principal_amount=row["top_decreased_other_shares"],
                    change_in_share=row["top_decreased_other_change_in_share"],
                    percent_change=row["top_decreased_other_percent_change"],
                    price_per_unit=row["top_decreased_other_price"],
                    change_type="decreased",
                )
                if row.get("top_decreased_other_issuer")
                else None
            )

            story_summaries.append(
                StorySummary(
                    cik=row["cik"],
                    aum=row["aum"],
                    latest_accession_number=row["latest_accession_number"],
                    previous_accession_number=row["previous_accession_number"],
                    company_name=row["company_name"],
                    reporting_period=row["reporting_period"],
                    filing_date=row["filing_date"],
                    top_new_position=top_new,
                    top_closed_position=top_closed,
                    top_increased_position=top_increased,
                    top_decreased_position=top_decreased,
                    top_new_other_securities=top_new_other,
                    top_closed_other_securities=top_closed_other,
                    top_increased_other_securities=top_increased_other,
                    top_decreased_other_securities=top_decreased_other,
                )
            )

        return LatestStoriesResponse(
            stories=story_summaries,
            has_next_page=has_next_page,
            pagination=pagination,
        )

    except Exception as e:
        logger.error(f"Error fetching latest stories v2: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


def _fetch_latest_activity(
    db: psycopg2.extensions.cursor,
    limit: int = 3,
    offset: int = 0,
    security_type: str = "stocks",
    ticker: Optional[str] = None,
    cik: Optional[str] = None,
    put_or_call: Optional[str] = None,
    change_type: Optional[str] = None,
    min_value: Optional[float] = None,
    sort_by: Optional[str] = "filing_date",
    sort_direction: Optional[str] = "desc",
    response: Optional[Response] = None,
) -> LatestActivityResponse:
    """
    Unified activity fetching engine for both stocks and options.
    Handles candidate selection, comparison queries, identifier resolution, and response formatting.
    """
    actual_limit = limit if isinstance(limit, int) else 3
    actual_offset = offset if isinstance(offset, int) else 0
    sec_type = (security_type or "stocks").strip().lower()

    if sec_type not in ("stocks", "options"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid security_type '{security_type}'. Allowed values: 'stocks', 'options'.",
        )

    allowed_sort_by = {
        "filing_date",
        "value_change",
        "absolute_value_change",
        "percent_change",
        "weight_pct",
        "company_name",
    }
    clean_sort_by = (sort_by or "filing_date").strip().lower()
    if clean_sort_by not in allowed_sort_by:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sort_by option '{sort_by}'. Allowed values: 'filing_date', 'value_change', 'percent_change', 'weight_pct', 'company_name'.",
        )

    clean_sort_dir = (sort_direction or "desc").strip().lower()
    if clean_sort_dir not in {"asc", "desc"}:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sort_direction '{sort_direction}'. Allowed values: 'asc', 'desc'.",
        )

    dir_upper = clean_sort_dir.upper()
    if clean_sort_by in ("value_change", "absolute_value_change"):
        order_by_clause = f"absolute_value_change {dir_upper} NULLS LAST, hc.filing_date DESC, hc.created_at DESC"
    elif clean_sort_by == "percent_change":
        order_by_clause = f"percent_change {dir_upper} NULLS LAST, hc.filing_date DESC, hc.created_at DESC"
    elif clean_sort_by == "weight_pct":
        order_by_clause = f"weight_pct {dir_upper} NULLS LAST, hc.filing_date DESC, hc.created_at DESC"
    elif clean_sort_by == "company_name":
        order_by_clause = f"hc.company_name {dir_upper}, hc.filing_date DESC, hc.created_at DESC"
    else:
        order_by_clause = f"hc.filing_date {dir_upper}, hc.created_at {dir_upper}"

    if put_or_call is not None and isinstance(put_or_call, str) and put_or_call.strip():
        poc_clean = put_or_call.strip().upper()
        if poc_clean == "PUT":
            put_or_call_patterns = ["PUT"]
        elif poc_clean == "CALL":
            put_or_call_patterns = ["CALL"]
        else:
            raise HTTPException(
                status_code=400,
                detail="Invalid put_or_call filter. Use 'PUT' or 'CALL'.",
            )
    else:
        put_or_call_patterns = ["PUT", "CALL"]

    valid_change_types = {"new", "closed", "increased", "decreased"}
    if change_type is not None and isinstance(change_type, str) and change_type.strip():
        ct_clean = change_type.strip().lower()
        if ct_clean not in valid_change_types:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid change_type '{change_type}'. Allowed values: {', '.join(sorted(valid_change_types))}.",
            )
        allowed_change_types = [ct_clean]
    else:
        allowed_change_types = ["new", "closed", "increased", "decreased"]

    target_cusip = None
    if ticker is not None and isinstance(ticker, str) and ticker.strip():
        _, resolved_cusip = resolve_identifiers(ticker.strip(), db)
        if not resolved_cusip:
            raise HTTPException(
                status_code=400,
                detail=f"Could not resolve identifier '{ticker}' to a valid CUSIP.",
            )
        target_cusip = resolved_cusip.upper()

    target_company_id = None
    if cik is not None and isinstance(cik, str) and cik.strip():
        raw_cik = cik.strip()
        clean_cik = raw_cik.lstrip("0") or "0"
        db.execute(
            "SELECT company_id FROM companies WHERE cik_number = %s OR cik_number = %s LIMIT 1",
            (raw_cik, clean_cik),
        )
        comp_row = db.fetchone()
        if not comp_row:
            raise HTTPException(
                status_code=404,
                detail=f"Manager with CIK '{cik}' not found.",
            )
        target_company_id = comp_row["company_id"]

    actual_min_value = min_value if isinstance(min_value, (int, float)) else None
    min_value_filter = (
        "AND ABS(COALESCE(hc.current_value, 0) - COALESCE(hc.previous_value, 0)) >= %(min_value)s"
        if actual_min_value is not None
        else ""
    )

    try:
        if sec_type == "options":
            query_params = {
                "limit": actual_limit + 1,
                "offset": actual_offset,
                "put_or_call_patterns": put_or_call_patterns,
            }
            if target_company_id:
                query_params["target_company_id"] = target_company_id
                candidate_filter = "AND f.company_id = %(target_company_id)s"
                candidate_limit = ""
                if target_cusip:
                    activity_cusip_filter = "AND UPPER(i.cusip) = %(target_cusip)s"
                    query_params["target_cusip"] = target_cusip
                    latest_filter = ""
                else:
                    activity_cusip_filter = ""
                    latest_filter = """AND EXISTS (
                        SELECT 1 FROM holdings h
                        JOIN put_or_call_table p ON h.put_or_call = p.id
                        WHERE h.filing_id = rf.filing_id
                          AND p.name ILIKE ANY(%(put_or_call_patterns)s)
                    )"""

                options_filing_query = FILING_QUERY_OPTIONS.format(
                    candidate_filter=candidate_filter,
                    candidate_limit=candidate_limit,
                    latest_filter=latest_filter,
                )
                db.execute(options_filing_query, query_params)
                candidate_filings = db.fetchall()

                has_next_page = len(candidate_filings) > actual_limit
                valid_filings = candidate_filings[:actual_limit]
            elif target_cusip:
                # Optimized two-step approach: resolve to integer IDs, then use
                # composite index (idx_holdings_issuer_put_call) for fast lookup.
                # This avoids scanning all 288k filings with an EXISTS subquery.

                # Step 1: Resolve CUSIP -> issuer_id(s)
                db.execute(
                    "SELECT issuer_id FROM issuers WHERE UPPER(cusip) = %s;",
                    (target_cusip,),
                )
                issuer_rows = db.fetchall()
                if not issuer_rows:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Could not resolve CUSIP '{target_cusip}' to any issuer.",
                    )
                target_issuer_ids = [r["issuer_id"] for r in issuer_rows]

                # Step 2: Resolve put_or_call pattern names -> integer IDs
                db.execute(
                    "SELECT id FROM put_or_call_table WHERE name ILIKE ANY(%s);",
                    (put_or_call_patterns,),
                )
                poc_id_rows = db.fetchall()
                poc_ids = [r["id"] for r in poc_id_rows]

                # Step 3: Get distinct filing_ids via composite index (pure integer predicates)
                db.execute(
                    "SELECT DISTINCT filing_id FROM holdings WHERE issuer_id = ANY(%s) AND put_or_call = ANY(%s);",
                    (target_issuer_ids, poc_ids),
                )
                matched_filing_ids = [r["filing_id"] for r in db.fetchall()]

                if not matched_filing_ids:
                    candidate_filings = []
                else:
                    # Step 4: Rank per company using only matched filing_ids
                    query_params["matched_filing_ids"] = matched_filing_ids
                    candidate_filter = "AND f.filing_id = ANY(%(matched_filing_ids)s)"
                    candidate_limit = ""
                    latest_filter = ""

                    options_filing_query = FILING_QUERY_OPTIONS.format(
                        candidate_filter=candidate_filter,
                        candidate_limit=candidate_limit,
                        latest_filter=latest_filter,
                    )
                    db.execute(options_filing_query, query_params)
                    candidate_filings = db.fetchall()

                activity_cusip_filter = "AND UPPER(i.cusip) = %(target_cusip)s"
                query_params["target_cusip"] = target_cusip

                has_next_page = len(candidate_filings) > actual_limit
                valid_filings = candidate_filings[:actual_limit]
            else:
                candidate_filter = ""
                candidate_limit = "LIMIT 10000"
                latest_filter = """AND EXISTS (
                    SELECT 1 FROM holdings h
                    JOIN put_or_call_table p ON h.put_or_call = p.id
                    WHERE h.filing_id = rf.filing_id
                      AND p.name ILIKE ANY(%(put_or_call_patterns)s)
                )"""
                activity_cusip_filter = ""

                options_filing_query = FILING_QUERY_OPTIONS.format(
                    candidate_filter=candidate_filter,
                    candidate_limit=candidate_limit,
                    latest_filter=latest_filter,
                )
                db.execute(options_filing_query, query_params)
                candidate_filings = db.fetchall()

                has_next_page = len(candidate_filings) > actual_limit
                valid_filings = candidate_filings[:actual_limit]
        else:
            stock_params = {"limit": actual_limit + 1, "offset": actual_offset}
            if target_company_id:
                filing_query_v2_fmt = FILING_QUERY_V2.replace(
                    "f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')",
                    "f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A') AND f.company_id = %(target_company_id)s",
                ).replace("LIMIT 2000", "")
                stock_params["target_company_id"] = target_company_id
            else:
                filing_query_v2_fmt = FILING_QUERY_V2

            db.execute(filing_query_v2_fmt, stock_params)
            candidate_filings = db.fetchall()

            has_next_page = len(candidate_filings) > actual_limit
            candidates_to_process = candidate_filings[:actual_limit]

            if not candidates_to_process:
                valid_filings = []
            else:
                candidate_filing_ids = [f["filing_id"] for f in candidates_to_process]
                params = {
                    "candidate_filing_ids": candidate_filing_ids,
                    "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
                }
                db.execute(FILTER_CANDIDATES_QUERY_V2, params)
                valid_filing_ids_set = {row["filing_id"] for row in db.fetchall()}
                valid_filings = [
                    f for f in candidates_to_process if f["filing_id"] in valid_filing_ids_set
                ]

        next_offset = actual_offset + actual_limit if has_next_page else None
        pagination = PaginationMetadata(
            limit=actual_limit,
            offset=actual_offset,
            total=None,
            has_more=has_next_page,
            next_offset=next_offset,
        )

        if response:
            response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=120"
            response.headers["X-Has-More"] = str(has_next_page).lower()
            if next_offset is not None:
                response.headers["X-Next-Offset"] = str(next_offset)
            response.headers["X-Limit"] = str(actual_limit)
            response.headers["X-Offset"] = str(actual_offset)

        if not valid_filings:
            return LatestActivityResponse(
                activities=[],
                has_next_page=has_next_page,
                pagination=pagination,
            )

        valid_filing_ids = {f["filing_id"] for f in valid_filings}
        filing_ids_to_process = set()
        filing_data_tuples = []

        for f in valid_filings:
            filing_ids_to_process.add(f["filing_id"])
            if f["previous_filing_id"]:
                filing_ids_to_process.add(f["previous_filing_id"])

            filing_data_tuples.append(
                (
                    f["filing_id"],
                    f["company_id"],
                    f["accession_number"],
                    f["period_of_report"],
                    f["filing_date"],
                    f["created_at"],
                    f["company_name"],
                    f["cik_number"],
                    f["aum"],
                    f["previous_filing_id"],
                    f["previous_accession_number"],
                    f.get("form_type"),
                )
            )

        values_string_list = []
        for t in filing_data_tuples:
            values_string_list.append(
                db.mogrify("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", t).decode("utf-8")
            )
        values_string = ",\n".join(values_string_list)

        if sec_type == "options":
            final_query_template = LATEST_ACTIVITY_QUERY_OPTIONS.format(
                cusip_filter=activity_cusip_filter,
                min_value_filter=min_value_filter,
                order_by_clause=order_by_clause,
                form_type_col=", form_type",
                form_type_expr="fwp.form_type",
            ).replace("%s", values_string)

            final_query_params = {
                "valid_filing_ids": list(valid_filing_ids),
                "filing_ids_to_process": list(filing_ids_to_process),
                "put_or_call_patterns": put_or_call_patterns,
                "allowed_change_types": allowed_change_types,
            }
            if target_cusip:
                final_query_params["target_cusip"] = target_cusip
            if actual_min_value is not None:
                final_query_params["min_value"] = actual_min_value

            db.execute(final_query_template, final_query_params)
            results = db.fetchall()[:1000]
        else:
            final_query_template = LATEST_ACTIVITY_QUERY_V3.format(
                order_by_clause=order_by_clause,
                min_value_filter=min_value_filter,
                form_type_col=", form_type",
                form_type_expr="fwp.form_type",
            ).replace("%s", values_string)
            final_query_params = {
                "valid_filing_ids": list(valid_filing_ids),
                "filing_ids_to_process": list(filing_ids_to_process),
                "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
            }
            if actual_min_value is not None:
                final_query_params["min_value"] = actual_min_value

            db.execute(final_query_template, final_query_params)
            results = db.fetchall()[:1000]

        activities = []
        for row in results:
            activity_dict = dict(row)
            clean_cusip = activity_dict["cusip"].upper() if activity_dict.get("cusip") else None
            activity_dict["cusip"] = clean_cusip
            activity_dict["ticker"] = (
                CUSIP_TO_TICKER.get(clean_cusip)
                or CUSIP_TO_TICKER.get(activity_dict.get("cusip"))
            )
            activity_dict["security_type"] = sec_type
            activities.append(HoldingActivity(**activity_dict))

        return LatestActivityResponse(
            activities=activities,
            has_next_page=has_next_page,
            pagination=pagination,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch latest activity ({sec_type}): {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/activity/latest/v3", response_model=LatestActivityResponse)
@router.get("/activity/latest/v3/", response_model=LatestActivityResponse, include_in_schema=False)
def get_latest_activity_v3(
    request: Request,
    limit: int = Query(3, description="Number of *companies* to fetch stories for", ge=1, le=50),
    offset: int = Query(0, description="Number of *companies* to skip for pagination", ge=0),
    security_type: Optional[str] = Query("stocks", description="Filter by security type: 'stocks' or 'options'"),
    ticker: Optional[str] = Query(None, description="Filter by underlying stock ticker or CUSIP (e.g. AAPL, NVDA)"),
    cik: Optional[str] = Query(None, description="Filter by company CIK number (e.g. 0001067983)"),
    put_or_call: Optional[str] = Query(None, description="Filter by option contract type ('PUT' or 'CALL')"),
    change_type: Optional[str] = Query(None, description="Filter by trade type ('new', 'closed', 'increased', 'decreased')"),
    min_value: Optional[float] = Query(None, description="Filter by minimum absolute dollar value change", ge=0),
    sort_by: Optional[str] = Query("filing_date", description="Sort by field: 'filing_date', 'value_change', 'percent_change', 'weight_pct', 'company_name'"),
    sort_direction: Optional[str] = Query("desc", description="Sort direction: 'asc' or 'desc'"),
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieves a flat list of all significant holding changes (headlines)
    from the latest batch of company filings.
    """
    return _fetch_latest_activity(
        db=db,
        limit=limit,
        offset=offset,
        security_type=security_type or "stocks",
        ticker=ticker,
        cik=cik,
        put_or_call=put_or_call,
        change_type=change_type,
        min_value=min_value,
        sort_by=sort_by,
        sort_direction=sort_direction,
        response=response,
    )


@router.get("/activity/latest/options", response_model=LatestActivityResponse)
@router.get("/activity/latest/options/", response_model=LatestActivityResponse, include_in_schema=False)
def get_latest_options_activity(
    request: Request,
    limit: int = Query(10, description="Number of *companies* to fetch options trades for", ge=1, le=50),
    offset: int = Query(0, description="Number of *companies* to skip for pagination", ge=0),
    ticker: Optional[str] = Query(None, description="Filter by underlying stock ticker or CUSIP (e.g. AAPL, NVDA)"),
    cik: Optional[str] = Query(None, description="Filter by company CIK number (e.g. 0001067983)"),
    put_or_call: Optional[str] = Query(None, description="Filter by option contract type ('PUT' or 'CALL')"),
    change_type: Optional[str] = Query(None, description="Filter by trade type ('new', 'closed', 'increased', 'decreased')"),
    min_value: Optional[float] = Query(None, description="Filter by minimum absolute dollar value change", ge=0),
    sort_by: Optional[str] = Query("filing_date", description="Sort by field: 'filing_date', 'value_change', 'percent_change', 'weight_pct', 'company_name'"),
    sort_direction: Optional[str] = Query("desc", description="Sort direction: 'asc' or 'desc'"),
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieves institutional options (Put/Call) trades from the latest batch of filings.
    """
    return _fetch_latest_activity(
        db=db,
        limit=limit,
        offset=offset,
        security_type="options",
        ticker=ticker,
        cik=cik,
        put_or_call=put_or_call,
        change_type=change_type,
        min_value=min_value,
        sort_by=sort_by,
        sort_direction=sort_direction,
        response=response,
    )
