"""
change_feed.py

Pull side of the Content Hub sync contract: a single, source-less, cursor-
paginated feed of complete 13F filings. Each item is one filing with ALL of its
holdings embedded (common stock AND options, every position tagged with
``is_common_stock`` / ``put_or_call``). Slicing, faceting and per-holding
explosion are Content Hub's concern — secapi hands over the whole record.

Cursor: an opaque token over the composite ``(updated_at, filing_id)`` keyset.
``updated_at`` is DB-stamped on every insert AND update (migration
``006_change_cursor.sql``), so revisions of an already-served filing re-surface;
``filing_id`` breaks ties. A ``lag_seconds`` high-water mark hides the unsettled
head so a concurrent, out-of-order commit cannot be skipped.
"""

import base64
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from config import CUSIP_TO_TICKER
from sec_models import FilingEnvelope, HoldingActivity

FORM_TYPES = ("13F-HR", "13F-HR/A", "13F-HR/A/A")

# Default / cap for filings-per-page. Kept low because each filing embeds ALL of
# its holdings, so a page of fat filings is large; response gzip does the rest.
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
DEFAULT_LAG_SECONDS = 5


# ---------------------------------------------------------------------------
# Opaque composite cursor  <iso8601-utc>_<filing_id>  (base64url)
# ---------------------------------------------------------------------------
def _iso_utc(value: datetime) -> str:
    """Normalise a datetime to a UTC microsecond ISO-8601 string with a Z."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def encode_cursor(updated_at: datetime, filing_id: int) -> str:
    raw = f"{_iso_utc(updated_at)}_{int(filing_id)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


# Cursor for "nothing yet" — an epoch marker a consumer can tail forward from.
EMPTY_CURSOR = encode_cursor(datetime(1970, 1, 1, tzinfo=timezone.utc), 0)


def decode_cursor(token: str) -> Tuple[str, int]:
    """
    Decode an opaque cursor into ``(iso_timestamp, filing_id)``.
    Raises ``ValueError`` on any malformed token (the router maps this to 400).
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad cursor
        raise ValueError("Invalid cursor") from exc

    iso_str, sep, id_str = raw.rpartition("_")
    if not sep:
        raise ValueError("Invalid cursor")
    try:
        filing_id = int(id_str)
        if filing_id < 0:
            raise ValueError
        # Validate the timestamp half parses (Postgres will re-parse it too).
        datetime.strptime(iso_str.rstrip("Z"), "%Y-%m-%dT%H:%M:%S.%f")
    except ValueError as exc:
        raise ValueError("Invalid cursor") from exc
    return iso_str, filing_id


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
# Keyset page of filings. {keyset} is injected as a bound-parameter predicate,
# never a value. The predecessor LATERAL resolves the prior 13F for diffing.
FILING_PAGE_QUERY = """
    SELECT
        f.filing_id,
        f.company_id,
        f.accession_number,
        f.period_of_report,
        f.filing_date,
        f.created_at,
        f.updated_at,
        f.form_type,
        f.file_number,
        f.filing_directory,
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
    WHERE f.form_type IN %(forms)s
      AND f.updated_at <= now() - make_interval(secs => %(lag)s)
      {keyset}
    ORDER BY f.updated_at {order}, f.filing_id {order}
    LIMIT %(limit)s;
"""

# Unified holdings diff for a page of filings. Unlike the endpoint-specific
# stock/option queries this classifies EVERY position (no put/call exclusion),
# tags it with is_common_stock + put_or_call, and does NOT gate on a predecessor
# existing — a first-ever filing surfaces with all holdings as 'new'.
CHANGE_FEED_ACTIVITY_QUERY = """
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
            MIN(i.issuer_name) AS issuer_name,
            SUM(h.value) AS total_value,
            SUM(h.shares_or_principal_amount) AS total_shares,
            tc.is_common_stock,
            -- Normalise put_or_call: the source dictionary holds duplicate case
            -- variants (PUT/CALL and Put/Call). Uppercasing collapses them so a
            -- filing labelled 'Call' does not diff against a predecessor's 'CALL'
            -- as a spurious new/closed pair, and Content Hub receives clean values.
            -- upper(NULL) stays NULL, so stock holdings are unaffected.
            upper(poc.name) AS put_or_call
        FROM holdings_normalised h
        JOIN issuers i ON h.issuer_id = i.issuer_id
        JOIN title_of_class_table tc ON h.title_of_class = tc.id
        LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
        WHERE h.filing_id = ANY(%(filing_ids_to_process)s)
        GROUP BY h.filing_id, i.cusip, tc.is_common_stock, upper(poc.name)
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
            hc.change_type,
            hc.is_common_stock
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
                END AS change_type,
                COALESCE(curr.is_common_stock, prev.is_common_stock) AS is_common_stock
            FROM
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.filing_id) AS curr
            FULL OUTER JOIN
                (SELECT * FROM AggregatedHoldings WHERE filing_id = fwp.previous_filing_id) AS prev
                ON curr.cusip = prev.cusip
               AND curr.is_common_stock = prev.is_common_stock
               AND curr.put_or_call IS NOT DISTINCT FROM prev.put_or_call
            WHERE (curr.cusip IS NOT NULL OR prev.cusip IS NOT NULL)
        ) hc ON true
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
            CASE WHEN hc.current_shares > 0 THEN (hc.current_value::numeric) / hc.current_shares ELSE 0 END, 2
        ) AS current_price_per_share,
        ROUND(
            CASE WHEN hc.previous_shares > 0 THEN (hc.previous_value::numeric) / hc.previous_shares ELSE 0 END, 2
        ) AS previous_price_per_share,
        ROUND(
            CASE WHEN hc.aum > 0 AND hc.current_value > 0 THEN (hc.current_value::numeric / hc.aum) * 100 ELSE NULL END, 4
        ) AS weight_pct,
        ROUND(
            CASE
                WHEN hc.previous_value > 0 AND hc.current_value IS NOT NULL THEN
                    ((hc.current_value::numeric - hc.previous_value::numeric) / hc.previous_value::numeric) * 100
                ELSE NULL
            END, 2
        ) AS value_pct
    FROM HoldingsComparison hc
    WHERE hc.change_type IN ('new', 'closed', 'increased', 'decreased')
    ORDER BY hc.filing_date DESC, hc.created_at DESC;
"""


# ---------------------------------------------------------------------------
# Fetch orchestration
# ---------------------------------------------------------------------------
def fetch_head(db, lag_seconds: float = DEFAULT_LAG_SECONDS) -> str:
    """Opaque cursor of the newest settled 13F filing, for tailing from 'now'."""
    db.execute(
        """
        SELECT updated_at, filing_id
        FROM filings
        WHERE form_type IN %(forms)s
          AND updated_at <= now() - make_interval(secs => %(lag)s)
        ORDER BY updated_at DESC, filing_id DESC
        LIMIT 1;
        """,
        {"forms": FORM_TYPES, "lag": lag_seconds},
    )
    row = db.fetchone()
    if not row:
        return EMPTY_CURSOR
    return encode_cursor(row["updated_at"], row["filing_id"])


def fetch_changes(
    db,
    cursor: Optional[str],
    limit: int,
    lag_seconds: float = DEFAULT_LAG_SECONDS,
    direction: str = "forward",
) -> Tuple[List[FilingEnvelope], Optional[str], bool]:
    """
    One keyset page of complete filings.

    ``direction`` = ``"forward"`` walks newer than the cursor (``(updated_at,
    filing_id) >`` cursor, ASC); ``"backward"`` walks older (``<`` cursor, DESC).
    With no cursor, forward starts at the oldest and backward at the newest head.

    Returns ``(items, next_cursor, has_more)`` where:
      * ``next_cursor`` = the last row's cursor on ANY non-empty page — resume in
        the SAME direction to continue; ``None`` only when the page is empty.
      * ``has_more`` = ``len(page) == limit`` (a separate "more right now" hint).

    Raises ``ValueError`` for a malformed cursor.
    """
    # direction drives both the keyset comparator and the sort order. Only
    # 'backward' flips it; any other value is treated as forward. These are
    # code-controlled literals, never user text, so splicing them is injection-safe.
    backward = direction == "backward"
    comparator = "<" if backward else ">"
    order = "DESC" if backward else "ASC"

    named = {"forms": FORM_TYPES, "lag": lag_seconds, "limit": limit}
    keyset = ""
    if cursor:
        cur_ts, cur_id = decode_cursor(cursor)  # raises ValueError -> 400
        named["cur_ts"] = cur_ts
        named["cur_id"] = cur_id
        keyset = f"AND (f.updated_at, f.filing_id) {comparator} (%(cur_ts)s::timestamptz, %(cur_id)s)"

    db.execute(FILING_PAGE_QUERY.format(keyset=keyset, order=order), named)
    filings = db.fetchall()
    if not filings:
        return [], None, False

    envelopes = {}
    filing_ids_to_process = set()
    filing_tuples = []
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
            file_number=f["file_number"],
            filing_directory=f["filing_directory"],
            created_at=f["created_at"],
            updated_at=f["updated_at"],
            previous_filing_id=f["previous_filing_id"],
            previous_accession_number=f["previous_accession_number"],
            activities=[],
        )
        filing_ids_to_process.add(f["filing_id"])
        if f["previous_filing_id"]:
            filing_ids_to_process.add(f["previous_filing_id"])
        filing_tuples.append(
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

    values_string = ",\n".join(
        db.mogrify("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", t).decode("utf-8")
        for t in filing_tuples
    )
    # The mogrified literals are spliced into a query that is executed AGAIN with
    # named params, so any literal '%' in a value (e.g. a fund named "50% Fund")
    # must be doubled to survive psycopg2's second placeholder pass.
    values_string = values_string.replace("%", "%%")
    # Every filing on the page is processed (including first-ever filings with no
    # predecessor), so they never appear to "skip" the cursor.
    valid_filing_ids = [f["filing_id"] for f in filings]

    final_query = CHANGE_FEED_ACTIVITY_QUERY.replace("%s", values_string, 1)
    db.execute(
        final_query,
        {
            "valid_filing_ids": valid_filing_ids,
            "filing_ids_to_process": list(filing_ids_to_process),
        },
    )
    for row in db.fetchall():
        acc = row["latest_accession_number"]
        env = envelopes.get(acc)
        if env is not None:
            d = dict(row)
            d["ticker"] = CUSIP_TO_TICKER.get(d["cusip"])
            env.activities.append(HoldingActivity(**d))

    last = filings[-1]
    next_cursor = encode_cursor(last["updated_at"], last["filing_id"])
    has_more = len(filings) == limit
    return list(envelopes.values()), next_cursor, has_more
