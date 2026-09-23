import json
import asyncio
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from config import logger
from database import get_db_cursor, get_db_connection, INTERNAL_ERROR_DETAIL
from sec_models import ChangesHeadResponse, ChangesResponse
import change_feed
from change_feed import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    DEFAULT_LAG_SECONDS,
    fetch_changes,
    fetch_head,
)

router = APIRouter()


@router.get("/changes/head", response_model=ChangesHeadResponse, tags=["Sync"])
def get_changes_head(
    lag_seconds: float = Query(
        DEFAULT_LAG_SECONDS, ge=0, le=3600, description="Hide filings settled less than this many seconds ago"
    ),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Opaque cursor of the newest settled 13F filing. Lets a consumer begin tailing
    live filings from 'now' (pass it as `cursor` to `GET /changes`) without
    crawling the ~288k historical filings first.
    """
    try:
        return ChangesHeadResponse(head_cursor=fetch_head(db, lag_seconds))
    except Exception:
        logger.error("Failed to fetch changes head", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/changes", response_model=ChangesResponse, tags=["Sync"])
def get_changes_feed(
    cursor: Optional[str] = Query(None, description="Opaque cursor from a prior page's next_cursor"),
    limit: int = Query(
        DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Max filings per page (each carries all its holdings)"
    ),
    lag_seconds: float = Query(
        DEFAULT_LAG_SECONDS, ge=0, le=3600, description="Hide filings settled less than this many seconds ago"
    ),
    direction: str = Query(
        "forward",
        pattern="^(forward|backward)$",
        description="Walk newer than the cursor (forward) or older (backward); resume in the same direction",
    ),
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Source-less, cursor-paginated feed of complete 13F filings for Content Hub.

    Each item is one filing with ALL of its holdings embedded in `activities[]`
    (common stock AND options, every position tagged `is_common_stock` /
    `put_or_call`). Content Hub owns the slicing, faceting and per-holding
    explosion. Keyset walks `(updated_at, filing_id)` so revisions re-surface;
    `next_cursor` advances on any non-empty page and is null only when empty.
    """
    try:
        items, next_cursor, has_more = fetch_changes(db, cursor, limit, lag_seconds, direction)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid cursor. Expected an opaque cursor from a prior page's next_cursor.",
        )
    except Exception:
        logger.error("Failed to fetch changes feed", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)

    if response:
        response.headers["Cache-Control"] = "no-cache"
    return ChangesResponse(items=items, next_cursor=next_cursor, has_more=has_more)


async def _fetch_head_async(lag_seconds: float) -> str:
    """One-shot head read on a short-lived connection (for the SSE greeting)."""

    def _run() -> str:
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                return fetch_head(cur, lag_seconds)
        except Exception:
            logger.warning("change_signal: head read failed for /stream greeting", exc_info=True)
            return change_feed.EMPTY_CURSOR
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    return await asyncio.to_thread(_run)


@router.get("/stream", tags=["Sync"])
async def stream_changes(
    request: Request,
    once: bool = Query(False, description="Emit the greeting frame and close (health checks / tests)"),
    replay_latest: bool = Query(False, description="Replay the last change signal on connect"),
    lag_seconds: float = Query(DEFAULT_LAG_SECONDS, ge=0, le=3600),
):
    """
    Server-Sent Events doorbell. Emits `event: connected` with the current head
    cursor, then `event: change` (payload from the DB NOTIFY) whenever a 13F
    filing is inserted or revised. Driven by Postgres LISTEN/NOTIFY — no polling.
    """
    broker = getattr(request.app.state, "signal_broker", None)
    head = await _fetch_head_async(lag_seconds)

    async def event_generator():
        yield f"event: connected\ndata: {json.dumps({'head': head})}\n\n"
        if once or broker is None:
            return

        queue = broker.subscribe()
        try:
            if replay_latest and broker.latest:
                yield f"event: change\ndata: {broker.latest}\n\n"
            while True:
                if await request.is_disconnected():
                    logger.info("SSE client disconnected from /stream")
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # keep-alive
                    continue
                yield f"event: change\ndata: {payload}\n\n"
        finally:
            broker.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            # Opt out of GZipMiddleware: it buffers chunks, which would defeat the
            # SSE doorbell's immediate delivery. Starlette's gzip passes through
            # any response that already declares a Content-Encoding.
            "Content-Encoding": "identity",
        },
    )
