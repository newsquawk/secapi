import json
import asyncio
from typing import Optional
import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from config import COMMON_STOCK_TITLE_OF_CLASS, CUSIP_TO_TICKER, logger
import database
from database import (
    get_db_cursor,
    get_db_connection,
    _is_connection_healthy,
    init_db_pool,
    INTERNAL_ERROR_DETAIL,
)
from routers.activity import (
    LATEST_ACTIVITY_QUERY_V3,
    LATEST_ACTIVITY_QUERY_OPTIONS,
)
from sec_models import (
    ChangesHeadResponse,
    ChangesResponse,
    FilingEnvelope,
    HoldingActivity,
)

router = APIRouter()


@router.get("/changes/head", response_model=ChangesHeadResponse, tags=["Sync"])
def get_changes_head(db: psycopg2.extensions.cursor = Depends(get_db_cursor)):
    """
    Returns the latest filing ID (head cursor) currently in PostgreSQL.
    Enables consumers (such as content-hub) to begin tailing live filings
    from 'now' without crawling historical filings.
    """
    try:
        db.execute("SELECT COALESCE(MAX(filing_id), 0) AS head_cursor FROM filings;")
        row = db.fetchone()
        head = str(row["head_cursor"] if row else 0)
        return ChangesHeadResponse(head_cursor=head)
    except Exception as e:
        logger.error("Failed to fetch changes head", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/changes/{source}", response_model=ChangesResponse, tags=["Sync"])
def get_changes_feed(
    request: Request,
    source: str,
    cursor: Optional[str] = Query(None, description="Starting cursor (exclusive filing_id)"),
    limit: int = Query(50, ge=1, le=100, description="Max filings per page"),
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Cursor-paginated change feed for content-hub.
    Yields filing envelopes containing activities[] with weight_pct, value_pct, and form_type.
    """
    source_clean = source.strip().lower()
    is_options = source_clean in ("options", "13f-options", "newsquawk-sec-filings-options")
    valid_sources = (
        "stocks",
        "13f-stocks",
        "newsquawk-sec-filings-stocks",
        "options",
        "13f-options",
        "newsquawk-sec-filings-options",
        "default",
    )
    if source_clean not in valid_sources:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source '{source}'. Valid sources are: {', '.join(valid_sources)}",
        )

    cursor_id = 0
    if cursor is not None and cursor.strip():
        try:
            cursor_id = int(cursor.strip())
            if cursor_id < 0:
                raise ValueError()
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid cursor. Expected non-negative numeric filing_id.",
            )

    try:
        filing_query = """
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
                pf.filing_id AS previous_filing_id,
                pf.accession_number AS previous_accession_number
            FROM filings f
            JOIN companies c ON f.company_id = c.company_id
            LEFT JOIN LATERAL (
                SELECT filing_id, accession_number
                FROM filings
                WHERE company_id = f.company_id
                  AND period_of_report < f.period_of_report
                  AND form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
                ORDER BY period_of_report DESC, filing_date DESC
                LIMIT 1
            ) pf ON true
            WHERE f.filing_id > %s
              AND f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
            ORDER BY f.filing_id ASC
            LIMIT %s;
        """
        db.execute(filing_query, (cursor_id, limit))
        filings = db.fetchall()

        if not filings:
            if response:
                response.headers["Cache-Control"] = "no-cache"
            return ChangesResponse(items=[], next_cursor=None, has_more=False)

        envelopes = {}
        filing_ids_to_process = set()
        filing_data_tuples = []

        for f in filings:
            acc = f["accession_number"]
            envelopes[acc] = FilingEnvelope(
                filing_id=f["filing_id"],
                accession_number=acc,
                cik=f["cik_number"],
                company_name=f["company_name"],
                form_type=f["form_type"],
                filing_date=f["filing_date"],
                period_of_report=f["period_of_report"],
                aum=f["aum"],
                previous_filing_id=f["previous_filing_id"],
                previous_accession_number=f["previous_accession_number"],
                activities=[],
            )
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

        values_string_list = [
            db.mogrify("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", t).decode("utf-8")
            for t in filing_data_tuples
        ]
        values_string = ",\n".join(values_string_list)
        valid_filing_ids = [f["filing_id"] for f in filings if f["previous_filing_id"]]

        if is_options:
            final_query = LATEST_ACTIVITY_QUERY_OPTIONS.format(
                cusip_filter="", min_value_filter=""
            ).replace("%s", values_string)
            final_params = {
                "valid_filing_ids": list(valid_filing_ids),
                "filing_ids_to_process": list(filing_ids_to_process),
                "put_or_call_patterns": ["put", "call"],
                "allowed_change_types": ["new", "closed", "increased", "decreased"],
            }
        else:
            final_query = LATEST_ACTIVITY_QUERY_V3.replace("%s", values_string)
            final_params = {
                "valid_filing_ids": list(valid_filing_ids),
                "filing_ids_to_process": list(filing_ids_to_process),
                "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
            }

        db.execute(final_query, final_params)
        activity_rows = db.fetchall()

        for row in activity_rows:
            acc = row["latest_accession_number"]
            if acc in envelopes:
                d = dict(row)
                d["ticker"] = CUSIP_TO_TICKER.get(d["cusip"])
                envelopes[acc].activities.append(HoldingActivity(**d))

        next_cursor = str(filings[-1]["filing_id"]) if len(filings) == limit else None
        has_more = next_cursor is not None

        if response:
            response.headers["Cache-Control"] = "no-cache"

        return ChangesResponse(
            items=list(envelopes.values()),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch changes feed for {source}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/stream", tags=["Sync"])
async def stream_changes(
    request: Request,
    once: bool = Query(False, description="Emit initial connection frame and close (for health checks / testing)"),
):
    """
    Server-Sent Events (SSE) doorbell endpoint for content-hub.
    Maintains a lightweight event stream and signals when new filings arrive.
    """
    async def async_get_max_filing_id(min_id: int = 0) -> int:
        def _query():
            pool = database.db_pool
            if pool is None:
                try:
                    init_db_pool()
                    pool = database.db_pool
                except Exception:
                    pass

            conn = None
            is_pooled = False
            try:
                if pool is not None:
                    candidate = pool.getconn()
                    if _is_connection_healthy(candidate):
                        conn = candidate
                        is_pooled = True
                    else:
                        pool.putconn(candidate, close=True)
                        conn = get_db_connection()
                        is_pooled = False
                else:
                    conn = get_db_connection()

                with conn.cursor() as cur:
                    if min_id > 0:
                        cur.execute("SELECT COALESCE(MAX(filing_id), 0) FROM filings WHERE filing_id > %s;", (min_id,))
                    else:
                        cur.execute("SELECT COALESCE(MAX(filing_id), 0) FROM filings;")
                    row = cur.fetchone()
                    return row[0] if row else 0
            except Exception as e:
                logger.warning(f"Error querying max filing_id in stream: {e}")
                if conn and is_pooled and pool is not None:
                    try:
                        pool.putconn(conn, close=True)
                        conn = None
                    except Exception:
                        pass
                return 0
            finally:
                if conn:
                    if is_pooled and pool is not None:
                        try:
                            conn.rollback()
                            pool.putconn(conn)
                        except Exception:
                            try:
                                pool.putconn(conn, close=True)
                            except Exception:
                                pass
                    else:
                        try:
                            conn.close()
                        except Exception:
                            pass
        return await asyncio.to_thread(_query)

    async def event_generator():
        last_head = await async_get_max_filing_id(0)
        yield f"event: connected\ndata: {json.dumps({'head': str(last_head)})}\n\n"

        if once:
            return

        iteration = 0
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected from /stream")
                break

            await asyncio.sleep(2)
            iteration += 1

            try:
                newest_id = await async_get_max_filing_id(last_head)
                if newest_id > last_head:
                    last_head = newest_id
                    yield f"event: change\ndata: {json.dumps({'source': 'stocks', 'head': str(newest_id)})}\n\n"
            except Exception as e:
                logger.warning(f"Error checking new filings in stream: {e}")

            if iteration % 10 == 0:
                yield ": keepalive\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
