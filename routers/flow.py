import datetime as dt
from typing import Optional
import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from config import COMMON_STOCK_TITLE_OF_CLASS, CUSIP_TO_TICKER, logger
from database import get_db_cursor, INTERNAL_ERROR_DETAIL
from sec_models import (
    DailyFlowEntry,
    DailyFlowResponse,
    AggregateFlowResponse,
    TopStockChangeEntry,
    TopStockChangesResponse,
    QuarterlyFlowEntry,
    QuarterlyFlowResponse,
)
from utils import resolve_identifiers, get_free_float

router = APIRouter()


@router.get("/api/v1/flow/daily/{identifier}", response_model=DailyFlowResponse)
def get_daily_stock_flow(
    request: Request,
    identifier: str,
    days: int = Query(1, description="Number of days to look back", ge=1, le=365),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Aggregates buying/selling flow based on the FILING DATE.
    This shows 'what was reported today' rather than 'what happened in Q3'.
    """
    ticker, clean_cusip = resolve_identifiers(identifier, db)

    if not clean_cusip:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve {identifier} to a valid CUSIP for analysis.",
        )

    logger.info(
        f"Daily Flow: Requested {identifier} -> Resolved Ticker: {ticker}, CUSIP: {clean_cusip}"
    )

    start_date = dt.date.today() - dt.timedelta(days=days)

    query = """
    WITH TargetIssuers AS (
        SELECT issuer_id FROM issuers WHERE UPPER(cusip) = %(cusip)s
    ),
    RecentFilings AS (
        SELECT f.filing_id, f.company_id, f.filing_date, f.period_of_report
        FROM filings f
        WHERE f.filing_date >= %(start_date)s
          AND f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ),
    PreviousFilings AS (
        SELECT
            rf.filing_id AS current_filing_id,
            rf.company_id,
            rf.filing_date,
            rf.period_of_report,
            (
                SELECT f2.filing_id
                FROM filings f2
                WHERE f2.company_id = rf.company_id
                  AND f2.period_of_report < rf.period_of_report
                  AND f2.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
                ORDER BY f2.period_of_report DESC, f2.filing_date DESC, f2.accession_number DESC
                LIMIT 1
            ) AS previous_filing_id
        FROM RecentFilings rf
    ),
    HoldingsComparison AS (
        SELECT
            pf.company_id,
            pf.filing_date,
            pf.period_of_report,
            COALESCE(curr.shares, 0) AS current_shares,
            COALESCE(prev.shares, 0) AS raw_previous_shares,
            ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint AS rebased_previous_shares,
            CASE
                WHEN prev.shares IS NULL OR prev.shares = 0 THEN 'new'
                WHEN curr.shares IS NULL OR curr.shares = 0 THEN 'closed'
                WHEN ABS(COALESCE(curr.shares, 0) - ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint) <= 1 THEN 'unchanged'
                WHEN COALESCE(curr.shares, 0) > ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint THEN 'increased'
                WHEN COALESCE(curr.shares, 0) < ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint THEN 'decreased'
                ELSE 'unchanged'
            END AS change_type
        FROM PreviousFilings pf
        LEFT JOIN LATERAL (
            SELECT SUM(h.shares_or_principal_amount) AS shares
            FROM holdings_normalised h
            JOIN title_of_class_table tc ON h.title_of_class = tc.id
            LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
            WHERE h.filing_id = pf.current_filing_id
              AND h.issuer_id IN (SELECT issuer_id FROM TargetIssuers)
              AND tc.is_common_stock = TRUE
              AND (poc.name IS NULL OR UPPER(poc.name) NOT IN ('PUT', 'CALL'))
              AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1)
        ) curr ON TRUE
        LEFT JOIN LATERAL (
            SELECT SUM(h.shares_or_principal_amount) AS shares
            FROM holdings_normalised h
            JOIN title_of_class_table tc ON h.title_of_class = tc.id
            LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
            WHERE h.filing_id = pf.previous_filing_id
              AND h.issuer_id IN (SELECT issuer_id FROM TargetIssuers)
              AND tc.is_common_stock = TRUE
              AND (poc.name IS NULL OR UPPER(poc.name) NOT IN ('PUT', 'CALL'))
              AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1)
        ) prev ON TRUE
        LEFT JOIN stock_splits ss
            ON ss.cusip = %(cusip)s
           AND ss.period_of_report = pf.period_of_report
        WHERE curr.shares > 0 OR prev.shares > 0
    )
    SELECT
        filing_date,
        SUM(CASE WHEN change_type IN ('new', 'increased') THEN (current_shares - rebased_previous_shares) ELSE 0 END) AS gross_buying,
        SUM(CASE WHEN change_type IN ('closed', 'decreased') THEN (rebased_previous_shares - current_shares) ELSE 0 END) AS gross_selling,
        SUM(CASE 
            WHEN change_type IN ('new', 'increased') THEN (current_shares - rebased_previous_shares)
            WHEN change_type IN ('closed', 'decreased') THEN -(rebased_previous_shares - current_shares)
            ELSE 0 
        END) AS net_change,
        SUM(rebased_previous_shares) AS total_previous_shares,
        COUNT(DISTINCT CASE WHEN change_type = 'new' THEN company_id END) AS funds_new,
        COUNT(DISTINCT CASE WHEN change_type = 'increased' THEN company_id END) AS funds_increased,
        COUNT(DISTINCT CASE WHEN change_type = 'decreased' THEN company_id END) AS funds_decreased,
        COUNT(DISTINCT CASE WHEN change_type = 'closed' THEN company_id END) AS funds_closed,
        COUNT(DISTINCT CASE WHEN change_type = 'unchanged' THEN company_id END) AS funds_unchanged
    FROM HoldingsComparison
    GROUP BY filing_date
    ORDER BY filing_date DESC;
    """

    try:
        db.execute(query, {"cusip": clean_cusip, "start_date": start_date})
        results = db.fetchall()

        daily_data = []
        for row in results:
            prev_shares = (
                float(row["total_previous_shares"])
                if row["total_previous_shares"]
                else 0.0
            )
            net_change = float(row["net_change"])

            pct_change = None
            if prev_shares > 0:
                pct_change = (net_change / prev_shares) * 100

            funds_new = int(row["funds_new"] or 0)
            funds_increased = int(row["funds_increased"] or 0)
            funds_decreased = int(row["funds_decreased"] or 0)
            funds_closed = int(row["funds_closed"] or 0)
            funds_unchanged = int(row["funds_unchanged"] or 0)
            funds_buying = funds_new + funds_increased
            funds_selling = funds_decreased + funds_closed

            daily_data.append(
                DailyFlowEntry(
                    date=row["filing_date"],
                    gross_buying=float(row["gross_buying"] or 0),
                    gross_selling=float(row["gross_selling"] or 0),
                    net_change=net_change,
                    percent_change=pct_change,
                    net_change_pct_float=None,
                    funds_buying=funds_buying,
                    funds_selling=funds_selling,
                    funds_new=funds_new,
                    funds_increased=funds_increased,
                    funds_decreased=funds_decreased,
                    funds_closed=funds_closed,
                    funds_unchanged=funds_unchanged,
                )
            )

        free_float_shares = None

        if ticker:
            float_in_millions = get_free_float(ticker)
            if float_in_millions and float_in_millions > 0:
                free_float_shares = float_in_millions * 1_000_000
                for entry in daily_data:
                    entry.net_change_pct_float = (
                        entry.net_change / free_float_shares
                    ) * 100

        return DailyFlowResponse(
            ticker=ticker,
            cusip=clean_cusip,
            free_float_shares=free_float_shares,
            daily_data=daily_data,
        )

    except Exception as e:
        logger.error(
            f"Error fetching daily stock flow for identifier {identifier}: {str(e)}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/api/v1/flow/aggregate/{identifier}", response_model=AggregateFlowResponse)
def get_aggregate_stock_flow(
    request: Request,
    identifier: str,
    days: int = Query(14, description="Number of days to look back", ge=1, le=365),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Aggregates total buying/selling flow over the last X days into a single summary.
    """
    ticker, clean_cusip = resolve_identifiers(identifier, db)

    if not clean_cusip:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve {identifier} to a valid CUSIP for analysis.",
        )

    logger.info(
        f"Aggregate Flow: Requested {identifier} -> Resolved Ticker: {ticker}, CUSIP: {clean_cusip}"
    )

    start_date = dt.date.today() - dt.timedelta(days=days)

    query = """
    WITH TargetIssuers AS (
        SELECT issuer_id FROM issuers WHERE UPPER(cusip) = %(cusip)s
    ),
    RecentFilings AS (
        SELECT f.filing_id, f.company_id, f.filing_date, f.period_of_report
        FROM filings f
        WHERE f.filing_date >= %(start_date)s
          AND f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ),
    PreviousFilings AS (
        SELECT
            rf.filing_id AS current_filing_id,
            rf.company_id,
            rf.filing_date,
            rf.period_of_report,
            (
                SELECT f2.filing_id
                FROM filings f2
                WHERE f2.company_id = rf.company_id
                  AND f2.period_of_report < rf.period_of_report
                  AND f2.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
                ORDER BY f2.period_of_report DESC, f2.filing_date DESC, f2.accession_number DESC
                LIMIT 1
            ) AS previous_filing_id
        FROM RecentFilings rf
    ),
    HoldingsComparison AS (
        SELECT
            pf.company_id,
            pf.filing_date,
            pf.period_of_report,
            COALESCE(curr.shares, 0) AS current_shares,
            COALESCE(prev.shares, 0) AS raw_previous_shares,
            ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint AS rebased_previous_shares,
            CASE
                WHEN prev.shares IS NULL OR prev.shares = 0 THEN 'new'
                WHEN curr.shares IS NULL OR curr.shares = 0 THEN 'closed'
                WHEN ABS(COALESCE(curr.shares, 0) - ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint) <= 1 THEN 'unchanged'
                WHEN COALESCE(curr.shares, 0) > ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint THEN 'increased'
                WHEN COALESCE(curr.shares, 0) < ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint THEN 'decreased'
                ELSE 'unchanged'
            END AS change_type
        FROM PreviousFilings pf
        LEFT JOIN LATERAL (
            SELECT SUM(h.shares_or_principal_amount) AS shares
            FROM holdings_normalised h
            JOIN title_of_class_table tc ON h.title_of_class = tc.id
            LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
            WHERE h.filing_id = pf.current_filing_id
              AND h.issuer_id IN (SELECT issuer_id FROM TargetIssuers)
              AND tc.is_common_stock = TRUE
              AND (poc.name IS NULL OR UPPER(poc.name) NOT IN ('PUT', 'CALL'))
              AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1)
        ) curr ON TRUE
        LEFT JOIN LATERAL (
            SELECT SUM(h.shares_or_principal_amount) AS shares
            FROM holdings_normalised h
            JOIN title_of_class_table tc ON h.title_of_class = tc.id
            LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
            WHERE h.filing_id = pf.previous_filing_id
              AND h.issuer_id IN (SELECT issuer_id FROM TargetIssuers)
              AND tc.is_common_stock = TRUE
              AND (poc.name IS NULL OR UPPER(poc.name) NOT IN ('PUT', 'CALL'))
              AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1)
        ) prev ON TRUE
        LEFT JOIN stock_splits ss
            ON ss.cusip = %(cusip)s
           AND ss.period_of_report = pf.period_of_report
        WHERE curr.shares > 0 OR prev.shares > 0
    )
    SELECT
        COALESCE(SUM(CASE WHEN change_type IN ('new', 'increased') THEN (current_shares - rebased_previous_shares) ELSE 0 END), 0) AS total_gross_buying,
        COALESCE(SUM(CASE WHEN change_type IN ('closed', 'decreased') THEN (rebased_previous_shares - current_shares) ELSE 0 END), 0) AS total_gross_selling,
        COALESCE(SUM(CASE 
            WHEN change_type IN ('new', 'increased') THEN (current_shares - rebased_previous_shares)
            WHEN change_type IN ('closed', 'decreased') THEN -(rebased_previous_shares - current_shares)
            ELSE 0 
        END), 0) AS total_net_change,
        COALESCE(SUM(rebased_previous_shares), 0) AS total_previous_shares,
        COUNT(DISTINCT CASE WHEN change_type = 'new' THEN company_id END) AS funds_new,
        COUNT(DISTINCT CASE WHEN change_type = 'increased' THEN company_id END) AS funds_increased,
        COUNT(DISTINCT CASE WHEN change_type = 'decreased' THEN company_id END) AS funds_decreased,
        COUNT(DISTINCT CASE WHEN change_type = 'closed' THEN company_id END) AS funds_closed,
        COUNT(DISTINCT CASE WHEN change_type = 'unchanged' THEN company_id END) AS funds_unchanged
    FROM HoldingsComparison;
    """

    try:
        db.execute(query, {"cusip": clean_cusip, "start_date": start_date})
        result = db.fetchone()

        funds_new = int(result["funds_new"] or 0) if result else 0
        funds_increased = int(result["funds_increased"] or 0) if result else 0
        funds_decreased = int(result["funds_decreased"] or 0) if result else 0
        funds_closed = int(result["funds_closed"] or 0) if result else 0
        funds_unchanged = int(result["funds_unchanged"] or 0) if result else 0
        funds_buying = funds_new + funds_increased
        funds_selling = funds_decreased + funds_closed

        if not result:
            return AggregateFlowResponse(
                ticker=ticker,
                cusip=clean_cusip,
                days_looked_back=days,
                gross_buying=0.0,
                gross_selling=0.0,
                net_change=0.0,
                net_change_pct_float=None,
                percent_change=None,
                free_float_shares=None,
                funds_buying=0,
                funds_selling=0,
                funds_new=0,
                funds_increased=0,
                funds_decreased=0,
                funds_closed=0,
                funds_unchanged=0,
            )

        net_change_val = float(result["total_net_change"])
        total_previous = float(result["total_previous_shares"])
        net_change_pct = None
        percent_change = None
        free_float_shares = None

        if ticker:
            float_in_millions = get_free_float(ticker)
            if float_in_millions and float_in_millions > 0:
                free_float_shares = float_in_millions * 1_000_000
                net_change_pct = (net_change_val / free_float_shares) * 100

        if total_previous > 0:
            percent_change = (net_change_val / total_previous) * 100

        return AggregateFlowResponse(
            ticker=ticker,
            cusip=clean_cusip,
            days_looked_back=days,
            gross_buying=float(result["total_gross_buying"]),
            gross_selling=float(result["total_gross_selling"]),
            net_change=net_change_val,
            net_change_pct_float=net_change_pct,
            percent_change=percent_change,
            free_float_shares=free_float_shares,
            funds_buying=funds_buying,
            funds_selling=funds_selling,
            funds_new=funds_new,
            funds_increased=funds_increased,
            funds_decreased=funds_decreased,
            funds_closed=funds_closed,
            funds_unchanged=funds_unchanged,
        )

    except Exception as e:
        logger.error(
            f"Error fetching aggregate stock flow for identifier {identifier}: {str(e)}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/api/v1/flow/top-changes", response_model=TopStockChangesResponse)
def get_top_market_changes_today(
    request: Request,
    date: Optional[dt.date] = Query(
        None, description="Target filing date (YYYY-MM-DD). Defaults to today."
    ),
    sort_by: str = Query(
        "value", description="Sort by 'value' (dollar change) or 'shares' (share count)"
    ),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieves the top 50 stocks with the largest absolute net change across
    all institutional 13F filings processed on a specific date.
    """
    target_date = date or dt.date.today()

    if sort_by not in ["value", "shares"]:
        raise HTTPException(
            status_code=400, detail="Invalid sort_by option. Use 'value' or 'shares'."
        )

    order_by_inner = (
        "ABS(SUM(hc.value_change))"
        if sort_by == "value"
        else "ABS(SUM(hc.share_change))"
    )
    order_by_outer = (
        "ABS(tm.net_value_change)"
        if sort_by == "value"
        else "ABS(tm.net_shares_change)"
    )

    query = f"""
    WITH RelevantFilings AS (
        SELECT f.filing_id, f.company_id, f.filing_date, f.period_of_report
        FROM filings f
        WHERE f.filing_date = %(date)s
          AND f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ),
    PreviousFilings AS (
        SELECT
            rf.filing_id AS current_filing_id,
            rf.company_id,
            rf.filing_date,
            rf.period_of_report,
            (
                SELECT f2.filing_id
                FROM filings f2
                WHERE f2.company_id = rf.company_id
                  AND f2.period_of_report < rf.period_of_report
                  AND f2.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
                ORDER BY f2.period_of_report DESC, f2.filing_date DESC, f2.accession_number DESC
                LIMIT 1
            ) AS previous_filing_id
        FROM RelevantFilings rf
    ),
    HoldingsComparison AS (
        SELECT
            pf.company_id,
            hc.cusip,
            hc.share_change,
            hc.value_change,
            hc.buying_shares,
            hc.selling_shares,
            hc.change_type
        FROM PreviousFilings pf
        LEFT JOIN LATERAL (
            SELECT
                COALESCE(curr.cusip, prev.cusip) AS cusip,
                CASE
                    WHEN prev.cusip IS NULL OR prev.previous_shares = 0 THEN 'new'
                    WHEN curr.cusip IS NULL OR curr.current_shares = 0 THEN 'closed'
                    WHEN ABS(COALESCE(curr.current_shares, 0) - ROUND(COALESCE(prev.previous_shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint) <= 1 THEN 'unchanged'
                    WHEN COALESCE(curr.current_shares, 0) > ROUND(COALESCE(prev.previous_shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint THEN 'increased'
                    WHEN COALESCE(curr.current_shares, 0) < ROUND(COALESCE(prev.previous_shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint THEN 'decreased'
                    ELSE 'unchanged'
                END AS change_type,
                CASE
                    WHEN prev.cusip IS NULL OR prev.previous_shares = 0 THEN COALESCE(curr.current_shares, 0)
                    WHEN curr.cusip IS NULL OR curr.current_shares = 0 THEN 0
                    WHEN ABS(COALESCE(curr.current_shares, 0) - ROUND(COALESCE(prev.previous_shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint) <= 1 THEN 0
                    WHEN COALESCE(curr.current_shares, 0) > ROUND(COALESCE(prev.previous_shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint
                        THEN (COALESCE(curr.current_shares, 0) - ROUND(COALESCE(prev.previous_shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint)
                    ELSE 0
                END AS buying_shares,
                CASE
                    WHEN prev.cusip IS NULL OR prev.previous_shares = 0 THEN 0
                    WHEN curr.cusip IS NULL OR curr.current_shares = 0 THEN ROUND(COALESCE(prev.previous_shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint
                    WHEN ABS(COALESCE(curr.current_shares, 0) - ROUND(COALESCE(prev.previous_shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint) <= 1 THEN 0
                    WHEN COALESCE(curr.current_shares, 0) < ROUND(COALESCE(prev.previous_shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint
                        THEN (ROUND(COALESCE(prev.previous_shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint - COALESCE(curr.current_shares, 0))
                    ELSE 0
                END AS selling_shares,
                CASE
                    WHEN ABS(COALESCE(curr.current_shares, 0) - ROUND(COALESCE(prev.previous_shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint) <= 1 
                         AND curr.cusip IS NOT NULL AND prev.cusip IS NOT NULL THEN 0
                    ELSE (COALESCE(curr.current_shares, 0) - ROUND(COALESCE(prev.previous_shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint)
                END AS share_change,
                CASE
                    WHEN ABS(COALESCE(curr.current_shares, 0) - ROUND(COALESCE(prev.previous_shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint) <= 1 
                         AND curr.cusip IS NOT NULL AND prev.cusip IS NOT NULL THEN 0
                    ELSE (COALESCE(curr.current_value, 0) - COALESCE(prev.previous_value, 0))
                END AS value_change
            FROM (
                SELECT 
                    UPPER(i.cusip) AS cusip,
                    SUM(h.shares_or_principal_amount) AS current_shares,
                    SUM(h.value) AS current_value 
                FROM holdings_normalised h
                JOIN issuers i ON h.issuer_id = i.issuer_id
                JOIN title_of_class_table tc ON h.title_of_class = tc.id
                LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
                WHERE h.filing_id = pf.current_filing_id
                  AND tc.is_common_stock = TRUE
                  AND (poc.name IS NULL OR UPPER(poc.name) NOT IN ('PUT', 'CALL'))
                  AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1)
                  AND i.cusip IS NOT NULL
                GROUP BY UPPER(i.cusip)
            ) curr
            FULL OUTER JOIN (
                SELECT 
                    UPPER(i.cusip) AS cusip,
                    SUM(h.shares_or_principal_amount) AS previous_shares,
                    SUM(h.value) AS previous_value 
                FROM holdings_normalised h
                JOIN issuers i ON h.issuer_id = i.issuer_id
                JOIN title_of_class_table tc ON h.title_of_class = tc.id
                LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
                WHERE h.filing_id = pf.previous_filing_id
                  AND tc.is_common_stock = TRUE
                  AND (poc.name IS NULL OR UPPER(poc.name) NOT IN ('PUT', 'CALL'))
                  AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1)
                  AND i.cusip IS NOT NULL
                GROUP BY UPPER(i.cusip)
            ) prev ON curr.cusip = prev.cusip
            LEFT JOIN stock_splits ss
                ON ss.cusip = COALESCE(curr.cusip, prev.cusip)
               AND ss.period_of_report = pf.period_of_report
        ) hc ON TRUE
    ),
    TopMoverCusips AS (
        SELECT
            hc.cusip,
            SUM(hc.share_change) AS net_shares_change,
            SUM(hc.value_change) AS net_value_change,
            SUM(ABS(hc.value_change)) AS absolute_value_change,
            SUM(hc.buying_shares) AS gross_buying_shares,
            SUM(hc.selling_shares) AS gross_selling_shares,
            COUNT(DISTINCT CASE WHEN hc.change_type = 'new' THEN hc.company_id END) AS funds_new,
            COUNT(DISTINCT CASE WHEN hc.change_type = 'increased' THEN hc.company_id END) AS funds_increased,
            COUNT(DISTINCT CASE WHEN hc.change_type = 'decreased' THEN hc.company_id END) AS funds_decreased,
            COUNT(DISTINCT CASE WHEN hc.change_type = 'closed' THEN hc.company_id END) AS funds_closed,
            COUNT(DISTINCT CASE WHEN hc.change_type = 'unchanged' THEN hc.company_id END) AS funds_unchanged
        FROM HoldingsComparison hc
        WHERE hc.cusip IS NOT NULL
        GROUP BY hc.cusip
        ORDER BY {order_by_inner} DESC
        LIMIT 50
    )
    SELECT
        tm.cusip,
        COALESCE(ci.issuer_name, tm.cusip) AS issuer_name,
        ci.symbol AS ticker,
        tm.net_shares_change,
        tm.net_value_change,
        tm.absolute_value_change,
        tm.gross_buying_shares,
        tm.gross_selling_shares,
        tm.funds_new,
        tm.funds_increased,
        tm.funds_decreased,
        tm.funds_closed,
        tm.funds_unchanged
    FROM TopMoverCusips tm
    LEFT JOIN LATERAL (
        SELECT i.issuer_name, i.symbol
        FROM issuers i
        WHERE i.cusip IN (tm.cusip, LOWER(tm.cusip))
        ORDER BY 
            (CASE WHEN i.symbol IS NOT NULL AND i.symbol NOT LIKE '%%.%%' THEN 1 
                  WHEN i.symbol IS NOT NULL THEN 2 
                  ELSE 3 END),
            LENGTH(i.issuer_name) DESC
        LIMIT 1
    ) ci ON TRUE
    ORDER BY {order_by_outer} DESC;
    """

    try:
        db.execute(
            query,
            {"date": target_date},
        )
        results = db.fetchall()

        stocks_data = []
        for row in results:
            clean_cusip = row["cusip"].upper() if row["cusip"] else None
            resolved_ticker = row["ticker"] or (
                CUSIP_TO_TICKER.get(clean_cusip) if clean_cusip else None
            )
            funds_new = int(row.get("funds_new", 0) or 0)
            funds_increased = int(row.get("funds_increased", 0) or 0)
            funds_decreased = int(row.get("funds_decreased", 0) or 0)
            funds_closed = int(row.get("funds_closed", 0) or 0)
            funds_unchanged = int(row.get("funds_unchanged", 0) or 0)
            funds_buying = funds_new + funds_increased
            funds_selling = funds_decreased + funds_closed

            stocks_data.append(
                TopStockChangeEntry(
                    issuer_name=row["issuer_name"],
                    cusip=clean_cusip,
                    ticker=resolved_ticker,
                    net_shares_change=float(row["net_shares_change"]),
                    net_value_change=float(row["net_value_change"]),
                    absolute_value_change=float(row["absolute_value_change"]),
                    gross_buying_shares=float(row["gross_buying_shares"]),
                    gross_selling_shares=float(row["gross_selling_shares"]),
                    funds_buying=funds_buying,
                    funds_selling=funds_selling,
                    funds_new=funds_new,
                    funds_increased=funds_increased,
                    funds_decreased=funds_decreased,
                    funds_closed=funds_closed,
                    funds_unchanged=funds_unchanged,
                )
            )

        return TopStockChangesResponse(
            date=target_date, sort_by=sort_by, stocks=stocks_data
        )

    except Exception as e:
        logger.error(f"Error compiling top market stock flows: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/api/v1/flow/quarterly/{identifier}", response_model=QuarterlyFlowResponse)
@router.get("/flow/quarterly/{identifier}", response_model=QuarterlyFlowResponse, include_in_schema=False)
def get_quarterly_stock_flow(
    request: Request,
    identifier: str,
    quarters: int = Query(8, description="Number of quarters to look back", ge=1, le=40),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Aggregates buying/selling flow and distinct fund counts grouped by reporting quarter (period_of_report).
    """
    ticker, clean_cusip = resolve_identifiers(identifier, db)

    if not clean_cusip:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve {identifier} to a valid CUSIP for analysis.",
        )

    logger.info(
        f"Quarterly Flow: Requested {identifier} -> Resolved Ticker: {ticker}, CUSIP: {clean_cusip}"
    )

    query = """
    WITH TargetIssuers AS (
        SELECT issuer_id FROM issuers WHERE UPPER(cusip) = %(cusip)s
    ),
    QuarterPeriods AS (
        SELECT DISTINCT period_of_report
        FROM filings
        WHERE period_of_report <= CURRENT_DATE
          AND form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
        ORDER BY period_of_report DESC
        LIMIT %(quarters)s
    ),
    MinPeriod AS (
        SELECT MIN(period_of_report) - INTERVAL '100 days' AS min_date FROM QuarterPeriods
    ),
    RelevantCompanies AS (
        SELECT DISTINCT f.company_id
        FROM holdings h
        JOIN filings f ON h.filing_id = f.filing_id
        WHERE h.issuer_id IN (SELECT issuer_id FROM TargetIssuers)
          AND f.period_of_report >= (SELECT min_date FROM MinPeriod)
          AND f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ),
    RecentFilings AS (
        SELECT DISTINCT ON (f.company_id, f.period_of_report)
            f.filing_id, f.company_id, f.filing_date, f.period_of_report
        FROM filings f
        JOIN QuarterPeriods qp ON f.period_of_report = qp.period_of_report
        JOIN RelevantCompanies rc ON f.company_id = rc.company_id
        WHERE f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
        ORDER BY f.company_id, f.period_of_report, f.filing_date DESC, f.accession_number DESC
    ),
    PreviousFilings AS (
        SELECT
            rf.filing_id AS current_filing_id,
            rf.company_id,
            rf.filing_date,
            rf.period_of_report,
            (
                SELECT f2.filing_id
                FROM filings f2
                WHERE f2.company_id = rf.company_id
                  AND f2.period_of_report < rf.period_of_report
                  AND f2.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
                ORDER BY f2.period_of_report DESC, f2.filing_date DESC, f2.accession_number DESC
                LIMIT 1
            ) AS previous_filing_id
        FROM RecentFilings rf
    ),
    HoldingsComparison AS (
        SELECT
            pf.company_id,
            pf.period_of_report,
            COALESCE(curr.shares, 0) AS current_shares,
            COALESCE(prev.shares, 0) AS raw_previous_shares,
            ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint AS rebased_previous_shares,
            CASE
                WHEN prev.shares IS NULL OR prev.shares = 0 THEN 'new'
                WHEN curr.shares IS NULL OR curr.shares = 0 THEN 'closed'
                WHEN ABS(COALESCE(curr.shares, 0) - ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint) <= 1 THEN 'unchanged'
                WHEN COALESCE(curr.shares, 0) > ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint THEN 'increased'
                WHEN COALESCE(curr.shares, 0) < ROUND(COALESCE(prev.shares, 0) * COALESCE(ss.split_factor, 1.0))::bigint THEN 'decreased'
                ELSE 'unchanged'
            END AS change_type
        FROM PreviousFilings pf
        LEFT JOIN LATERAL (
            SELECT SUM(h.shares_or_principal_amount) AS shares
            FROM holdings_normalised h
            JOIN title_of_class_table tc ON h.title_of_class = tc.id
            LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
            WHERE h.filing_id = pf.current_filing_id
              AND h.issuer_id IN (SELECT issuer_id FROM TargetIssuers)
              AND tc.is_common_stock = TRUE
              AND (poc.name IS NULL OR UPPER(poc.name) NOT IN ('PUT', 'CALL'))
              AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1)
        ) curr ON TRUE
        LEFT JOIN LATERAL (
            SELECT SUM(h.shares_or_principal_amount) AS shares
            FROM holdings_normalised h
            JOIN title_of_class_table tc ON h.title_of_class = tc.id
            LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
            WHERE h.filing_id = pf.previous_filing_id
              AND h.issuer_id IN (SELECT issuer_id FROM TargetIssuers)
              AND tc.is_common_stock = TRUE
              AND (poc.name IS NULL OR UPPER(poc.name) NOT IN ('PUT', 'CALL'))
              AND (h.shares_or_principal_type IS NULL OR h.shares_or_principal_type = 1)
        ) prev ON TRUE
        LEFT JOIN stock_splits ss
            ON ss.cusip = %(cusip)s
           AND ss.period_of_report = pf.period_of_report
        WHERE curr.shares > 0 OR prev.shares > 0
    )
    SELECT
        period_of_report AS quarter,
        SUM(CASE WHEN change_type IN ('new', 'increased') THEN (current_shares - rebased_previous_shares) ELSE 0 END) AS gross_buying,
        SUM(CASE WHEN change_type IN ('closed', 'decreased') THEN (rebased_previous_shares - current_shares) ELSE 0 END) AS gross_selling,
        SUM(CASE 
            WHEN change_type IN ('new', 'increased') THEN (current_shares - rebased_previous_shares)
            WHEN change_type IN ('closed', 'decreased') THEN -(rebased_previous_shares - current_shares)
            ELSE 0 
        END) AS net_change,
        SUM(rebased_previous_shares) AS total_previous_shares,
        COUNT(DISTINCT CASE WHEN change_type = 'new' THEN company_id END) AS funds_new,
        COUNT(DISTINCT CASE WHEN change_type = 'increased' THEN company_id END) AS funds_increased,
        COUNT(DISTINCT CASE WHEN change_type = 'decreased' THEN company_id END) AS funds_decreased,
        COUNT(DISTINCT CASE WHEN change_type = 'closed' THEN company_id END) AS funds_closed,
        COUNT(DISTINCT CASE WHEN change_type = 'unchanged' THEN company_id END) AS funds_unchanged
    FROM HoldingsComparison
    GROUP BY period_of_report
    ORDER BY period_of_report DESC;
    """

    try:
        db.execute(query, {"cusip": clean_cusip, "quarters": quarters})
        results = db.fetchall()

        quarterly_data = []
        for row in results:
            prev_shares = (
                float(row["total_previous_shares"])
                if row["total_previous_shares"]
                else 0.0
            )
            net_change = float(row["net_change"])

            pct_change = None
            if prev_shares > 0:
                pct_change = (net_change / prev_shares) * 100

            funds_new = int(row["funds_new"] or 0)
            funds_increased = int(row["funds_increased"] or 0)
            funds_decreased = int(row["funds_decreased"] or 0)
            funds_closed = int(row["funds_closed"] or 0)
            funds_unchanged = int(row["funds_unchanged"] or 0)
            funds_buying = funds_new + funds_increased
            funds_selling = funds_decreased + funds_closed

            quarterly_data.append(
                QuarterlyFlowEntry(
                    quarter=row["quarter"],
                    gross_buying=float(row["gross_buying"] or 0),
                    gross_selling=float(row["gross_selling"] or 0),
                    net_change=net_change,
                    percent_change=pct_change,
                    funds_buying=funds_buying,
                    funds_selling=funds_selling,
                    funds_new=funds_new,
                    funds_increased=funds_increased,
                    funds_decreased=funds_decreased,
                    funds_closed=funds_closed,
                    funds_unchanged=funds_unchanged,
                )
            )

        return QuarterlyFlowResponse(
            ticker=ticker,
            cusip=clean_cusip,
            quarterly_data=quarterly_data,
        )

    except Exception as e:
        logger.error(
            f"Error fetching quarterly stock flow for identifier {identifier}: {str(e)}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)
