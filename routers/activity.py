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
from sec_models import LatestActivityResponse, HoldingActivity
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


# ---------------------------------------------------------------------------
# SQL Queries
# ---------------------------------------------------------------------------
FILING_QUERY_V2 = """
WITH CandidateFilings AS (
    SELECT
        f.filing_id,
        f.company_id,
        f.accession_number,
        f.period_of_report,
        f.filing_date,
        f.created_at
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
        created_at
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

LATEST_ACTIVITY_QUERY_V3 = """
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
            fwp.aum,
            fwp.cik_number,
            fwp.company_name,
            fwp.accession_number,
            fwp.previous_accession_number,
            fwp.period_of_report,
            fwp.filing_date,
            fwp.created_at,
            (SELECT form_type FROM filings WHERE filing_id = fwp.filing_id) AS form_type,
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
    ORDER BY
        hc.filing_date DESC, hc.created_at DESC;
"""

FILING_QUERY_OPTIONS = """
WITH CandidateFilings AS (
    SELECT
        f.filing_id,
        f.company_id,
        f.accession_number,
        f.period_of_report,
        f.filing_date,
        f.created_at
    FROM
        filings f
    WHERE
        f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ORDER BY f.filing_date DESC, f.created_at DESC
    LIMIT 10000
),
RankedFilings AS (
    SELECT
        filing_id,
        company_id,
        accession_number,
        period_of_report,
        filing_date,
        created_at,
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
        rf.created_at
    FROM RankedFilings rf
    WHERE rf.rn = 1
      AND EXISTS (
          SELECT 1 FROM holdings h
          JOIN put_or_call_table p ON h.put_or_call = p.id
          {issuer_join}
          WHERE h.filing_id = rf.filing_id
            AND p.name ILIKE ANY(%(put_or_call_patterns)s)
            {cusip_filter}
      )
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

LATEST_ACTIVITY_QUERY_OPTIONS = """
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
            UPPER(i.cusip) AS cusip,
            p.name AS put_or_call,
            MIN(i.issuer_name) as issuer_name,
            SUM(h.value) as total_value,
            SUM(h.shares_or_principal_amount) as total_shares
        FROM holdings_normalised h
        JOIN issuers i ON h.issuer_id = i.issuer_id
        JOIN put_or_call_table p ON h.put_or_call = p.id
        WHERE h.filing_id = ANY(%(filing_ids_to_process)s)
          AND p.name ILIKE ANY(%(put_or_call_patterns)s)
          {cusip_filter}
        GROUP BY h.filing_id, UPPER(i.cusip), p.name
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
            (SELECT form_type FROM filings WHERE filing_id = fwp.filing_id) AS form_type,
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
        hc.filing_date DESC, hc.created_at DESC;
"""


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

        if not candidates_to_process:
            logger.info("No candidate filings found for the given limit and offset.")
            return LatestStoriesResponse(stories=[])

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
            return LatestStoriesResponse(stories=[])

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

        if response:
            response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=120"
        return LatestStoriesResponse(stories=story_summaries, has_next_page=has_next_page)

    except Exception as e:
        logger.error(f"Error fetching latest stories v2: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/activity/latest/v3", response_model=LatestActivityResponse)
@router.get("/activity/latest/v3/", response_model=LatestActivityResponse, include_in_schema=False)
def get_latest_activity_v3(
    request: Request,
    limit: int = Query(3, description="Number of *companies* to fetch stories for", ge=1, le=15),
    offset: int = Query(0, description="Number of *companies* to skip for pagination", ge=0),
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieves a flat list of all significant holding changes (headlines)
    from the latest batch of company filings.
    """
    try:
        db.execute(FILING_QUERY_V2, {"limit": limit + 1, "offset": offset})
        candidate_filings = db.fetchall()

        has_next_page = len(candidate_filings) > limit
        candidates_to_process = candidate_filings[:limit]

        if not candidates_to_process:
            return LatestActivityResponse(activities=[])

        candidate_filing_ids = [f["filing_id"] for f in candidates_to_process]
        params = {
            "candidate_filing_ids": candidate_filing_ids,
            "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
        }
        db.execute(FILTER_CANDIDATES_QUERY_V2, params)
        valid_filing_ids = {row["filing_id"] for row in db.fetchall()}

        valid_filings = [
            f for f in candidates_to_process if f["filing_id"] in valid_filing_ids
        ]

        if not valid_filings:
            return LatestActivityResponse(activities=[])

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

        final_query_template = LATEST_ACTIVITY_QUERY_V3.replace("%s", values_string)
        final_query_params = {
            "valid_filing_ids": list(valid_filing_ids),
            "filing_ids_to_process": list(filing_ids_to_process),
            "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
        }

        db.execute(final_query_template, final_query_params)
        results = db.fetchall()

        activities = []
        for row in results[:1000]:
            activity_dict = dict(row)
            activity_dict["ticker"] = CUSIP_TO_TICKER.get(activity_dict["cusip"])
            activities.append(HoldingActivity(**activity_dict))

        if response:
            response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=120"
        return LatestActivityResponse(activities=activities, has_next_page=has_next_page)

    except Exception as e:
        logger.error("Failed to fetch latest activity v3", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/activity/latest/options", response_model=LatestActivityResponse)
@router.get("/activity/latest/options/", response_model=LatestActivityResponse, include_in_schema=False)
def get_latest_options_activity(
    request: Request,
    limit: int = Query(10, description="Number of *companies* to fetch options trades for", ge=1, le=50),
    offset: int = Query(0, description="Number of *companies* to skip for pagination", ge=0),
    ticker: Optional[str] = Query(None, description="Filter by underlying stock ticker or CUSIP (e.g. AAPL, NVDA)"),
    put_or_call: Optional[str] = Query(None, description="Filter by option contract type ('PUT' or 'CALL')"),
    change_type: Optional[str] = Query(None, description="Filter by trade type ('new', 'closed', 'increased', 'decreased')"),
    min_value: Optional[float] = Query(None, description="Filter by minimum absolute dollar value change", ge=0),
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieves institutional options (Put/Call) trades from the latest batch of filings.
    """
    actual_limit = limit if isinstance(limit, int) else 10
    actual_offset = offset if isinstance(offset, int) else 0

    if put_or_call is not None and isinstance(put_or_call, str):
        poc_clean = put_or_call.strip().upper()
        if poc_clean == "PUT":
            put_or_call_patterns = ["Put", "PUT"]
        elif poc_clean == "CALL":
            put_or_call_patterns = ["Call", "CALL"]
        else:
            raise HTTPException(
                status_code=400,
                detail="Invalid put_or_call filter. Use 'PUT' or 'CALL'.",
            )
    else:
        put_or_call_patterns = ["Put", "Call", "PUT", "CALL"]

    valid_change_types = {"new", "closed", "increased", "decreased"}
    if change_type is not None and isinstance(change_type, str):
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
    if ticker is not None and isinstance(ticker, str):
        _, resolved_cusip = resolve_identifiers(ticker, db)
        if not resolved_cusip:
            raise HTTPException(
                status_code=400,
                detail=f"Could not resolve identifier '{ticker}' to a valid CUSIP.",
            )
        target_cusip = resolved_cusip

    if target_cusip:
        issuer_join = "JOIN issuers i ON h.issuer_id = i.issuer_id"
        candidate_cusip_filter = "AND i.cusip IN (%(target_cusip)s, LOWER(%(target_cusip)s))"
        activity_cusip_filter = "AND i.cusip IN (%(target_cusip)s, LOWER(%(target_cusip)s))"
    else:
        issuer_join = ""
        candidate_cusip_filter = ""
        activity_cusip_filter = ""

    actual_min_value = min_value if isinstance(min_value, (int, float)) else None
    min_value_filter = (
        "AND ABS(COALESCE(hc.current_value, 0) - COALESCE(hc.previous_value, 0)) >= %(min_value)s"
        if actual_min_value is not None
        else ""
    )

    try:
        options_filing_query = FILING_QUERY_OPTIONS.format(
            issuer_join=issuer_join,
            cusip_filter=candidate_cusip_filter,
        )
        query_params = {
            "limit": actual_limit + 1,
            "offset": actual_offset,
            "put_or_call_patterns": put_or_call_patterns,
        }
        if target_cusip:
            query_params["target_cusip"] = target_cusip

        db.execute(options_filing_query, query_params)
        candidate_filings = db.fetchall()

        has_next_page = len(candidate_filings) > actual_limit
        valid_filings = candidate_filings[:actual_limit]

        if not valid_filings:
            return LatestActivityResponse(activities=[], has_next_page=False)

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
                )
            )

        values_string_list = []
        for t in filing_data_tuples:
            values_string_list.append(
                db.mogrify("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", t).decode("utf-8")
            )
        values_string = ",\n".join(values_string_list)

        final_query_template = LATEST_ACTIVITY_QUERY_OPTIONS.format(
            cusip_filter=activity_cusip_filter, min_value_filter=min_value_filter
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
        results = db.fetchall()

        activities = []
        for row in results:
            activity_dict = dict(row)
            clean_cusip = activity_dict["cusip"].upper() if activity_dict.get("cusip") else None
            activity_dict["ticker"] = (
                CUSIP_TO_TICKER.get(clean_cusip)
                or CUSIP_TO_TICKER.get(activity_dict["cusip"])
            )
            activities.append(HoldingActivity(**activity_dict))

        if response:
            response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=120"
        return LatestActivityResponse(activities=activities, has_next_page=has_next_page)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to fetch latest options activity", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)
