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
    WITH TargetIssuer AS (
        SELECT issuer_id FROM issuers WHERE cusip = %s LIMIT 1
    ),
    RecentFilings AS (
        SELECT f.filing_id, f.company_id, f.filing_date, f.period_of_report
        FROM filings f
        WHERE f.filing_date >= %s
          AND f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ),
    PreviousFilings AS (
        SELECT
            rf.filing_id AS current_filing_id,
            rf.filing_date,
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
            pf.filing_date,
            COALESCE(h_curr.shares_or_principal_amount, 0) as current_shares,
            COALESCE(h_prev.shares_or_principal_amount, 0) as previous_shares
        FROM PreviousFilings pf
        LEFT JOIN holdings h_curr ON h_curr.filing_id = pf.current_filing_id 
                                 AND h_curr.issuer_id = (SELECT issuer_id FROM TargetIssuer)
        LEFT JOIN holdings h_prev ON h_prev.filing_id = pf.previous_filing_id 
                                 AND h_prev.issuer_id = (SELECT issuer_id FROM TargetIssuer)
        WHERE h_curr.shares_or_principal_amount IS NOT NULL
           OR h_prev.shares_or_principal_amount IS NOT NULL
    )
    SELECT
        filing_date,
        SUM(CASE WHEN current_shares > previous_shares THEN (current_shares - previous_shares) ELSE 0 END) as gross_buying,
        SUM(CASE WHEN current_shares < previous_shares THEN ABS(current_shares - previous_shares) ELSE 0 END) as gross_selling,
        SUM(current_shares - previous_shares) as net_change,
        SUM(previous_shares) as total_previous_shares
    FROM HoldingsComparison
    GROUP BY filing_date
    ORDER BY filing_date DESC;
    """

    try:
        db.execute(query, (clean_cusip, start_date))
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

            daily_data.append(
                DailyFlowEntry(
                    date=row["filing_date"],
                    gross_buying=float(row["gross_buying"]),
                    gross_selling=float(row["gross_selling"]),
                    net_change=net_change,
                    percent_change=pct_change,
                    net_change_pct_float=None,
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
    WITH TargetIssuer AS (
        SELECT issuer_id FROM issuers WHERE cusip = %s LIMIT 1
    ),
    RecentFilings AS (
        SELECT f.filing_id, f.company_id, f.filing_date, f.period_of_report
        FROM filings f
        WHERE f.filing_date >= %s
          AND f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ),
    PreviousFilings AS (
        SELECT
            rf.filing_id AS current_filing_id,
            rf.filing_date,
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
            pf.filing_date,
            COALESCE(h_curr.shares_or_principal_amount, 0) as current_shares,
            COALESCE(h_prev.shares_or_principal_amount, 0) as previous_shares
        FROM PreviousFilings pf
        LEFT JOIN holdings h_curr ON h_curr.filing_id = pf.current_filing_id 
                                 AND h_curr.issuer_id = (SELECT issuer_id FROM TargetIssuer)
        LEFT JOIN holdings h_prev ON h_prev.filing_id = pf.previous_filing_id 
                                 AND h_prev.issuer_id = (SELECT issuer_id FROM TargetIssuer)
        WHERE h_curr.shares_or_principal_amount IS NOT NULL
           OR h_prev.shares_or_principal_amount IS NOT NULL
    )
    SELECT
        COALESCE(SUM(CASE WHEN current_shares > previous_shares THEN (current_shares - previous_shares) ELSE 0 END), 0) as total_gross_buying,
        COALESCE(SUM(CASE WHEN current_shares < previous_shares THEN ABS(current_shares - previous_shares) ELSE 0 END), 0) as total_gross_selling,
        COALESCE(SUM(current_shares - previous_shares), 0) as total_net_change,
        COALESCE(SUM(previous_shares), 0) as total_previous_shares
    FROM HoldingsComparison;
    """

    try:
        db.execute(query, (clean_cusip, start_date))
        result = db.fetchone()

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
            hc.cusip,
            (hc.current_shares - hc.previous_shares) AS share_change,
            (hc.current_value - hc.previous_value) AS value_change,
            CASE WHEN hc.current_shares > hc.previous_shares THEN (hc.current_shares - hc.previous_shares) ELSE 0 END AS buying_shares,
            CASE WHEN hc.current_shares < hc.previous_shares THEN (hc.previous_shares - hc.current_shares) ELSE 0 END AS selling_shares
        FROM RelevantFilings rf
        JOIN PreviousFilings pf ON rf.filing_id = pf.current_filing_id
        LEFT JOIN LATERAL (
            SELECT
                COALESCE(curr.cusip, prev.cusip) AS cusip,
                COALESCE(curr.current_shares, 0) AS current_shares,
                COALESCE(prev.previous_shares, 0) AS previous_shares,
                COALESCE(curr.current_value, 0) AS current_value,
                COALESCE(prev.previous_value, 0) AS previous_value
            FROM (
                SELECT 
                    UPPER(i.cusip) AS cusip,
                    SUM(h.shares_or_principal_amount) AS current_shares,
                    SUM(h.value) AS current_value 
                FROM holdings_normalised h
                JOIN issuers i ON h.issuer_id = i.issuer_id
                JOIN title_of_class_table tc ON h.title_of_class = tc.id
                WHERE h.filing_id = pf.current_filing_id
                  AND tc.is_common_stock = TRUE
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
                WHERE h.filing_id = pf.previous_filing_id
                  AND tc.is_common_stock = TRUE
                  AND i.cusip IS NOT NULL
                GROUP BY UPPER(i.cusip)
            ) prev ON curr.cusip = prev.cusip
        ) hc ON TRUE
    ),
    TopMoverCusips AS (
        SELECT
            hc.cusip,
            SUM(hc.share_change) AS net_shares_change,
            SUM(hc.value_change) AS net_value_change,
            SUM(ABS(hc.value_change)) AS absolute_value_change,
            SUM(hc.buying_shares) AS gross_buying_shares,
            SUM(hc.selling_shares) AS gross_selling_shares
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
        tm.gross_selling_shares
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
            {"date": target_date, "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS},
        )
        results = db.fetchall()

        stocks_data = []
        for row in results:
            clean_cusip = row["cusip"].upper() if row["cusip"] else None
            resolved_ticker = row["ticker"] or (
                CUSIP_TO_TICKER.get(clean_cusip) if clean_cusip else None
            )
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
                )
            )

        return TopStockChangesResponse(
            date=target_date, sort_by=sort_by, stocks=stocks_data
        )

    except Exception as e:
        logger.error(f"Error compiling top market stock flows: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)
