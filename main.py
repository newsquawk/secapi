import psycopg2
from fastapi import FastAPI, Depends, HTTPException, Query, Body
import os
import uvicorn
import polars as pl
import asyncio
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from fastapi import HTTPException
from psycopg2.extras import RealDictCursor
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
import datetime as dt
import pandas as pd

from sec_models import (
    Filing,
    FlowResponse,
    LatestActivityResponse,
    HoldingActivity,
    DailyFlowEntry,
    DailyFlowResponse,
)
from sic import SIC_MAPPING


EDGAR_IDENTITY = os.getenv("EDGAR_IDENTITY", "26b610663e50@company.co.uk")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", None)

if DEEPSEEK_API_KEY:
    client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

DEFINED_SCHEMA = {
    "cik": pl.String,
    "company": pl.String,
    "filing_date": pl.Date,
    "accession_no": pl.String,
}

TICKER_TO_CUSIP = {}
try:
    mapping_df = pd.read_csv("ticker_cusip_mapping.csv")
    # Convert tickers to uppercase for case-insensitive lookup, and map to CUSIP
    TICKER_TO_CUSIP = dict(zip(mapping_df["ticker"].str.upper(), mapping_df["cusip"]))
except Exception as e:
    print(f"Warning: Could not load ticker_cusip_mapping.csv: {e}")

### FastAPI app setup ###
app = FastAPI(title="SEC API", version="1.0.0", debug=True)

origins_env = os.environ.get("CORS_ORIGINS", "").split(",")
allow_origins = [origin.strip() for origin in origins_env if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,  # Your frontend origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],  # Allow all headers
)


def get_db_connection():
    """
    Establishes and returns a new PostgreSQL database connection.
    """
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            database=os.getenv("DB_NAME", "sec"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", "password"),
            port=os.getenv("DB_PORT", "5432"),
        )
        return conn
    except psycopg2.OperationalError as e:
        # Raise an exception that FastAPI can handle
        raise HTTPException(status_code=500, detail=f"Database connection failed: {e}")


def get_db_cursor():
    """
    Dependency that yields a database cursor and ensures the connection
    is properly managed.
    """
    conn = get_db_connection()
    # Use RealDictCursor to get results as dictionaries
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        yield cursor
    finally:
        cursor.close()
        conn.close()


@app.get("/")
def read_root():
    return {"message": "Welcome to the SEC API"}


def _format_address(
    street1: str,
    street2: str,
    city: str,
    state: str,
    state_desc: str,
    zipcode: str,
) -> str:
    """Format address components into a structured address"""
    # Clean empty values
    street1 = street1 or ""
    street2 = street2 or ""
    city = city or ""
    state = state or ""
    state_desc = state_desc or ""
    zipcode = zipcode or ""

    # Create full address string
    address_parts = []
    if street1:
        address_parts.append(street1)
    if street2:
        address_parts.append(street2)
    if city:
        address_parts.append(city)
    if state:
        address_parts.append(state)
    if state_desc:
        address_parts.append(state_desc)
    if zipcode:
        address_parts.append(zipcode)

    full_address = ", ".join(address_parts)

    return full_address


@app.get("/managers/", response_model=list[dict])
def get_managers(
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
    limit: int = 100,
    offset: int = 0,
):
    """
    Retrieve all managers with pagination
    """
    query = """
            SELECT 
                cik_number,
                company_name, 
                company_phone,
                company_mail_street1,
                company_mail_street2,
                company_mail_city,
                company_mail_state,
                company_mail_state_desc,
                company_zipcode,
                company_business_street1,
                company_business_street2,
                company_business_city,
                company_business_state,
                company_business_state_desc,
                company_business_zipcode
            FROM companies
            LIMIT %s OFFSET %s
        """
    # Execute the query and fetch the results directly into a Pandas DataFrame
    db.execute(query, (limit, offset))
    results = db.fetchall()

    companies = []
    for row in results:
        # Use dictionary keys from RealDictCursor for cleaner access
        mailing_address = _format_address(
            row.get("company_mail_street1"),
            row.get("company_mail_street2"),
            row.get("company_mail_city"),
            row.get("company_mail_state"),
            row.get("company_mail_state_desc"),
            row.get("company_mail_zipcode"),
        )
        business_address = _format_address(
            row.get("company_business_street1"),
            row.get("company_business_street2"),
            row.get("company_business_city"),
            row.get("company_business_state"),
            row.get("company_business_state_desc"),
            row.get("company_business_zipcode"),
        )

        companies.append(
            {
                "cik": str(row.get("cik_number")),
                "company_name": row.get("company_name"),
                "company_phone": row.get("company_phone"),
                "mailing_address": mailing_address,
                "business_address": business_address,
            }
        )

    return companies


@app.get("/managers/{cik}", response_model=dict)
def get_manager(
    cik: str,
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieve a single manager by CIK from the database
    """

    query = """
        SELECT
            cik_number,
            company_name, 
            company_phone,
            company_mail_street1,
            company_mail_street2,
            company_mail_city,
            company_mail_state,
            company_mail_state_desc,
            company_zipcode,
            company_business_street1,
            company_business_street2,
            company_business_city,
            company_business_state,
            company_business_state_desc,
            company_business_zipcode
        FROM companies
        WHERE cik_number = %s
    """

    try:
        db.execute(query, (cik,))
        result = db.fetchone()

        if not result:
            raise HTTPException(
                status_code=404, detail=f"Manager with CIK {cik} not found"
            )

        mailing_address = _format_address(
            result.get("company_mail_street1"),
            result.get("company_mail_street2"),
            result.get("company_mail_city"),
            result.get("company_mail_state"),
            result.get("company_mail_state_desc"),
            result.get("company_mail_zipcode"),
        )
        business_address = _format_address(
            result.get("company_business_street1"),
            result.get("company_business_street2"),
            result.get("company_business_city"),
            result.get("company_business_state"),
            result.get("company_business_state_desc"),
            result.get("company_business_zipcode"),
        )

        manager_data = {
            "cik": str(result.get("cik_number")),
            "company_name": result.get("company_name"),
            "company_phone": result.get("company_phone"),
            "mailing_address": mailing_address,
            "business_address": business_address,
        }

        return manager_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {e}")


@app.get("/managers/{cik}/filings")
def get_manager_filings(
    cik: str,
    limit: int = Query(100, ge=1, le=1000, description="Number of results to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieve all filings for a specific manager by CIK number
    """

    try:
        db.execute("SELECT company_id FROM companies WHERE cik_number = %s", (cik,))
        company: dict = db.fetchone()  # type: ignore
        if not company:
            raise HTTPException(
                status_code=404, detail=f"Manager with CIK {cik} not found"
            )
        company_id = company["company_id"]  # type: ignore

        count_query = "SELECT COUNT(*) FROM filings WHERE company_id = %s"
        db.execute(count_query, (company_id,))
        total_count = db.fetchone()["count"]  # type: ignore

        if total_count == 0:
            return {
                "filings": [],
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "total": 0,
                    "has_more": False,
                    "next_offset": None,
                },
            }

        filings_query = """
            SELECT
                accession_number,
                form_type,
                filing_date,
                period_of_report,
                file_number,
                filing_directory,
                created_at,
                updated_at
            FROM filings
            WHERE company_id = %s
            ORDER BY filing_date DESC
            LIMIT %s OFFSET %s
        """
        db.execute(filings_query, (company_id, limit, offset))
        filings_data = db.fetchall()

        # Format the response using the Pydantic model
        filings = [Filing(**row) for row in filings_data]  # type: ignore

        has_more = (offset + len(filings)) < total_count

        return {
            "filings": filings,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total_count,
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {e}")


@app.get("/filings/", response_model=dict)
def get_filings(
    limit: int = Query(100, description="Number of items to return", ge=1, le=100),
    offset: int = Query(0, description="Number of items to skip", ge=0),
    sort_by: str = Query("filing_date", description="Column to sort by"),
    sort_order: str = Query("desc", description="Sort order: 'asc' or 'desc'"),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    allowed_sort_columns = {
        "company_name": "c.company_name",
        "cik_number": "c.cik_number",
        "form_type": "f.form_type",
        "accession_number": "f.accession_number",
        "filing_date": "f.filing_date",
        "period_of_report": "f.period_of_report",
        "created_at": "f.created_at",
        "aum": "c.aum",
    }

    if sort_by not in allowed_sort_columns:
        raise HTTPException(status_code=400, detail="Invalid sort column specified.")

    # --- 2. Security: Validate sort order ---
    if sort_order.lower() not in ["asc", "desc"]:
        raise HTTPException(
            status_code=400, detail="Invalid sort order. Use 'asc' or 'desc'."
        )

    # Get the safe, validated column name and order
    sort_column = allowed_sort_columns[sort_by]
    sort_order_str = sort_order.upper()

    try:
        # Get the total count of filings for pagination metadata
        if sort_by == "aum":
            # Logic: If we sort by AUM, we only want the LATEST filing for each company.
            # Otherwise, we see the same company listed 20 times with the same AUM.

            # 1. Count unique companies (instead of total filings)
            count_query = "SELECT COUNT(DISTINCT company_id) as count FROM filings"
            db.execute(count_query)
            total_count = db.fetchone()["count"]

            # 2. Query for Distinct Latest Filings sorted by AUM
            # We use DISTINCT ON to grab the latest, then wrap it to sort by AUM.
            filings_query = f"""
                WITH LatestFilings AS (
                    SELECT DISTINCT ON (f.company_id)
                        f.accession_number, f.form_type, f.filing_date, f.period_of_report,
                        f.file_number, f.filing_directory, f.created_at, f.updated_at,
                        c.company_name, c.cik_number, c.aum
                    FROM filings f
                    LEFT JOIN companies c ON f.company_id = c.company_id
                    -- Internal Sort: Ensure we grab the latest filing for the distinct logic
                    ORDER BY f.company_id, f.filing_date DESC, f.accession_number DESC
                )
                SELECT * FROM LatestFilings
                -- Final Sort: Order the distinct list by AUM
                ORDER BY NULLIF(aum, 0) {sort_order_str} NULLS LAST
                LIMIT %s OFFSET %s
            """

            db.execute(filings_query, (limit, offset))
            filings_data = db.fetchall()
        else:
            count_query = "SELECT COUNT(*) FROM filings"
            db.execute(count_query)
            total_count = db.fetchone()["count"]  # type: ignore

            order_by_clause = f"{sort_column} {sort_order_str}"
            if sort_by == "filing_date":
                order_by_clause += f", f.created_at {sort_order_str}"
            else:
                order_by_clause += ", f.filing_date DESC, f.created_at DESC"

            if total_count == 0:
                return {
                    "filings": [],
                    "pagination": {
                        "limit": limit,
                        "offset": offset,
                        "total": 0,
                        "has_more": False,
                    },
                    "sorting": {
                        "current_sort_by": sort_by,
                        "current_sort_order": sort_order,
                    },
                }

            filings_query = f"""
                SELECT
                    f.accession_number, f.form_type, f.filing_date, f.period_of_report,
                    f.file_number, f.filing_directory, f.created_at, f.updated_at,
                    c.company_name, c.cik_number, c.aum
                FROM filings f
                LEFT JOIN companies c ON f.company_id = c.company_id
                ORDER BY {order_by_clause}
                LIMIT %s OFFSET %s
            """

            db.execute(filings_query, (limit, offset))
            filings_data = db.fetchall()

        # Format the response using the Pydantic model
        filings = filings_data

        has_more = (offset + len(filings)) < total_count

        return {
            "filings": filings,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total_count,
                "has_more": has_more,
            },
            "sorting": {
                "current_sort_by": sort_by,
                "current_sort_order": sort_order,
            },
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.get("/filings/{accession_number}", response_model=dict)
def get_filing_by_accession(
    accession_number: str,
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):

    try:
        query = """
            SELECT
                f.accession_number,
                f.form_type,
                f.filing_date,
                f.period_of_report,
                f.file_number,
                f.filing_directory,
                f.created_at,
                f.updated_at,
                c.company_name,
                c.cik_number
            FROM filings f
            LEFT JOIN companies c ON f.company_id = c.company_id
            WHERE f.accession_number = %s
        """
        db.execute(query, (accession_number,))
        result = db.fetchone()

        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"Filing with accession number {accession_number} not found",
            )

        return result  # type: ignore

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.get("/holdings/{accession_number}", response_model=dict)
def get_holding_by_accession_number(
    accession_number: str,
    # limit: int = Query(100, ge=1, le=1000),
    # offset: int = Query(0, ge=0),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
    draw: int = Query(0, alias="draw"),
    start: int = Query(0, alias="start"),
    length: int = Query(10, alias="length"),
    search_value: Optional[str] = Query(None, alias="search[value]"),
    order_column_index: int = Query(0, alias="order[0][column]"),
    order_dir: str = Query("asc", alias="order[0][dir]"),
):
    """
    Retrieve a single holding by accession number
    """

    try:
        # Map DataTables column index to actual database column names
        column_map = {
            0: "i.issuer_name",
            1: "i.cusip",
            2: "t.name",
            3: "h.value",
            4: "h.shares_or_principal_amount",
            5: "s.name",
            6: "d.name",
            7: "p.name",
            8: "h.voting_authority_sole",
            9: "h.voting_authority_shared",
            10: "h.voting_authority_none",
        }
        order_by_column = column_map.get(order_column_index, "h.value")
        order_direction = "DESC" if order_dir == "desc" else "ASC"

        # Base query to get the total count of all records (before any filtering)
        count_query = """
            SELECT COUNT(*)
            FROM holdings h
            INNER JOIN filings f ON h.filing_id = f.filing_id
            WHERE f.accession_number = %s;
        """
        db.execute(count_query, (accession_number,))
        total_records = db.fetchone()["count"]  # type: ignore

        # Construct the WHERE clause for searching
        where_clause = "WHERE f.accession_number = %s"
        search_params = [accession_number]
        if search_value:
            where_clause += """
                AND (
                    i.issuer_name ILIKE %s OR
                    i.cusip ILIKE %s OR
                    t.name ILIKE %s
                )
            """
            search_pattern = f"%{search_value}%"
            search_params.extend([search_pattern, search_pattern, search_pattern])

        # Query to get the count after applying the search filter
        filtered_count_query = f"""
            SELECT COUNT(*)
            FROM holdings h
            INNER JOIN filings f ON h.filing_id = f.filing_id
            LEFT JOIN issuers i ON h.issuer_id = i.issuer_id
            LEFT JOIN title_of_class_table t ON h.title_of_class = t.id
            {where_clause};
        """
        db.execute(filtered_count_query, search_params)
        filtered_records = db.fetchone()["count"]

        # Main query to fetch the paginated, filtered, and sorted data
        holdings_query = f"""
            SELECT
                h.holding_id,
                i.issuer_name,
                t.name AS title_of_class,
                h.shares_or_principal_amount,
                s.name AS shares_or_principal_type,
                h.value,
                p.name AS put_or_call,
                d.name AS investment_discretion,
                h.voting_authority_sole,
                h.voting_authority_shared,
                h.voting_authority_none,
                i.cusip
            FROM holdings h
            INNER JOIN filings f ON h.filing_id = f.filing_id
            LEFT JOIN issuers i ON h.issuer_id = i.issuer_id
            LEFT JOIN title_of_class_table t ON h.title_of_class = t.id
            LEFT JOIN share_type_table s ON h.shares_or_principal_type = s.id
            LEFT JOIN put_or_call_table p ON h.put_or_call = p.id
            LEFT JOIN investment_discretion_table d ON h.investment_discretion = d.id
            {where_clause}
            ORDER BY {order_by_column} {order_direction}
            OFFSET %s LIMIT %s;
        """
        query_params = search_params + [start, length]
        db.execute(holdings_query, query_params)
        holdings_data = db.fetchall()

        formatted_data = [
            {
                "issuer_name": row.get("issuer_name"),
                "cusip": row.get("cusip"),
                "title_of_class": row.get("title_of_class"),
                "value": row.get("value"),
                "shares_or_principal_amount": row.get("shares_or_principal_amount"),
                "shares_or_principal_type": row.get("shares_or_principal_type"),
                "investment_discretion": row.get("investment_discretion"),
                "put_or_call": row.get("put_or_call"),
                "voting_authority_sole": row.get("voting_authority_sole"),
                "voting_authority_shared": row.get("voting_authority_shared"),
                "voting_authority_none": row.get("voting_authority_none"),
            }
            for row in holdings_data
        ]

        # Format the response for DataTables
        return {
            "draw": draw,
            "recordsTotal": total_records,
            "recordsFiltered": filtered_records,
            "data": formatted_data,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


class HoldingsRequest(BaseModel):
    new_holdings: List[Dict[str, Any]]
    closed_positions: List[Dict[str, Any]]
    increased_holdings: List[Dict[str, Any]]
    decreased_holdings: List[Dict[str, Any]]


@app.post("/api/ai_summary")
async def openai_call(request: HoldingsRequest = Body(...)):

    if not DEEPSEEK_API_KEY:
        return JSONResponse(
            content={"summary": "API key not configured"}, status_code=503
        )

    # Frontend POST request will be in dict format?
    new_holdings = pl.DataFrame(request.new_holdings)
    closed_positions = pl.DataFrame(request.closed_positions)
    increased_holdings = pl.DataFrame(request.increased_holdings)
    decreased_holdings = pl.DataFrame(request.decreased_holdings)

    new_holdings_top_5 = pl.DataFrame()
    closed_positions_top_5 = pl.DataFrame()
    increased_holdings_top_5 = pl.DataFrame()
    decreased_holdings_top_5 = pl.DataFrame()

    if not new_holdings.is_empty():
        new_holdings_top_5 = new_holdings.sort(
            ["total_value_latest", "total_shares_latest"],
            descending=True,
        ).head(5)

    if not closed_positions.is_empty():
        closed_positions_top_5 = closed_positions.sort(
            ["total_value_prev", "total_shares_prev"], descending=True
        ).head(5)

    if not increased_holdings.is_empty():
        increased_holdings_top_5 = increased_holdings.sort(
            ["change_in_share", "percent_change"], descending=True
        ).head(5)

    if not decreased_holdings.is_empty():
        decreased_holdings_top_5 = decreased_holdings.sort(
            [
                "change_in_share",
                "percent_change",
            ],
            descending=False,
        ).head(5)

    # preparation to call openai API
    new_holdings_dict = {
        row["issuer_name_clean"]: row["total_shares_latest"]
        for row in new_holdings_top_5.to_dicts()
    }
    closed_positions_dict = {
        row["issuer_name_clean_prev"]: row["total_shares_prev"]
        for row in closed_positions_top_5.to_dicts()
    }
    increased_holdings_dict = {
        row["issuer_name_clean"]: {
            k: v
            for k, v in row.items()
            if k not in ["issuer_name_clean", "issuer_name_clean_prev"]
        }
        for row in increased_holdings_top_5.to_dicts()
    }
    decreased_holdings_dict = {
        row["issuer_name_clean"]: {
            k: v
            for k, v in row.items()
            if k not in ["issuer_name_clean", "issuer_name_clean_prev"]
        }
        for row in decreased_holdings_top_5.to_dicts()
    }

    new_holding_text = "|".join([f"{k} {v}" for k, v in new_holdings_dict.items()])
    closed_positions_text = "|".join(
        [f"{k} {v}" for k, v in closed_positions_dict.items()]
    )
    increased_holdings_text = "|".join(
        [f"{k} {v}" for k, v in increased_holdings_dict.items()]
    )
    decreased_holdings_text = "|".join(
        [f"{k} {v}" for k, v in decreased_holdings_dict.items()]
    )

    async def get_summary(title, data_text):
        prompt = f"""
        Generate a summary of the holdings changes for the fund management in one or two sentences.
        {title}: {data_text}
        """
        # Assuming an async OpenAI client
        response = await client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "system",
                    "content": "You are a professional financial analyst. Be concise.",
                },
                {"role": "user", "content": prompt},
            ],
            stream=False,
        )
        return response.choices[0].message.content

    api_calls = [
        get_summary("New Holdings", new_holding_text),
        get_summary("Closed Positions", closed_positions_text),
        get_summary("Increased Holdings", increased_holdings_text),
        get_summary("Decreased Holdings", decreased_holdings_text),
    ]

    texts = await asyncio.gather(*api_calls)

    final_summary = " ".join([text for text in texts if text])
    return JSONResponse(content={"summary": final_summary})


def _df_to_dict_list(df: pl.DataFrame):
    """Convert DataFrame to list of dictionaries"""
    if df.is_empty():
        return []
    return df.to_dicts()


def run_in_threadpool(func, *args):
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, func, *args)


FILING_QUERY_WITH_PRIORITY = """
    SELECT
        f.accession_number,
        c.cik_number,
        c.company_name,
        f.form_type,
        f.filing_date,
        f.period_of_report,
        f.filing_directory
    FROM filings f
    JOIN companies c ON f.company_id = c.company_id
    WHERE
        c.cik_number = (SELECT c2.cik_number FROM filings f2 JOIN companies c2 ON f2.company_id = c2.company_id WHERE f2.accession_number = %s LIMIT 1)
    AND
        f.period_of_report = (SELECT f3.period_of_report FROM filings f3 WHERE f3.accession_number = %s LIMIT 1)
    ORDER BY
    CASE
        WHEN f.form_type = '13F-HR/A/A' THEN 1
        WHEN f.form_type = '13F-HR/A' THEN 2
        WHEN f.form_type = '13F-HR' THEN 3
        ELSE 4
    END,
    f.filing_date DESC
    LIMIT 1
"""

HOLDINGS_QUERY = """
    SELECT
        h.holding_id,
        i.issuer_name,
        t.name AS title_of_class,
        h.shares_or_principal_amount,
        s.name AS shares_or_principal_type,
        h.value,
        p.name AS put_or_call,
        d.name AS investment_discretion,
        h.voting_authority_sole,
        h.voting_authority_shared,
        h.voting_authority_none,
        i.cusip,
        i.sic
    FROM holdings h
    LEFT JOIN issuers i ON h.issuer_id = i.issuer_id
    LEFT JOIN title_of_class_table t ON h.title_of_class = t.id
    LEFT JOIN share_type_table s ON h.shares_or_principal_type = s.id
    LEFT JOIN put_or_call_table p ON h.put_or_call = p.id
    LEFT JOIN investment_discretion_table d ON h.investment_discretion = d.id
    WHERE h.filing_id = (SELECT filing_id FROM filings WHERE accession_number = %s)
    ORDER BY h.value DESC
"""

HOLDINGS_SCHEMA = {
    "holding_id": pl.Int64,
    "issuer_name": pl.Utf8,
    "title_of_class": pl.Utf8,
    "shares_or_principal_amount": pl.Int64,
    "shares_or_principal_type": pl.Utf8,
    "value": pl.Int64,
    "put_or_call": pl.Utf8,
    "investment_discretion": pl.Utf8,
    "voting_authority_sole": pl.Int64,
    "voting_authority_shared": pl.Int64,
    "voting_authority_none": pl.Int64,
    "cusip": pl.Utf8,
    "sic": pl.Int64,
}

COMMON_STOCK_TITLE_OF_CLASS = "COM|CL A|COMMON STOCK|STOCK|COM SHS|CAP STK CL"

MAX_PER_SHARE_PRICE = 11.0
MIN_PER_SHARE_PRICE = 0.1


def get_sic_division(sic_code):
    try:
        # Convert code to an integer for numerical comparison
        code = int(sic_code)
    except (ValueError, TypeError):
        return "Unclassified Code"

    if 0 <= code <= 999:
        # Range 0100-0999 in the table
        return "Agriculture, Forestry and Fishing"
    elif 1000 <= code <= 1499:
        return "Mining"
    elif 1500 <= code <= 1799:
        return "Construction"
    elif 1800 <= code <= 1999:
        return "Not Used"
    elif 2000 <= code <= 3999:
        return "Manufacturing"
    elif 4000 <= code <= 4999:
        return "Transportation, Communications, Electric, Gas and Sanitary Service"
    elif 5000 <= code <= 5199:
        return "Wholesale Trade"
    elif 5200 <= code <= 5999:
        return "Retail Trade"
    elif 6000 <= code <= 6799:
        return "Finance, Insurance and Real estate"
    elif 7000 <= code <= 8999:
        return "Services"
    elif 9100 <= code <= 9729:
        return "Public administration"
    elif 9900 <= code <= 9999:
        return "Non-Classifiable"
    else:
        # This handles codes that fall in the gaps (e.g., 9000, 9800)
        return "Unclassified Code"


def _process_filings(
    df: pl.DataFrame,
    latest=True,
):
    suffix = "latest" if latest else "prev"

    if df.is_empty():
        output_schema = {
            "cusip": pl.Utf8,
            "put_or_call": pl.Utf8,
            "issuer_name_clean": pl.Utf8,
            "sic": pl.Int64,
            f"total_shares_{suffix}": pl.Int64,
            f"total_value_{suffix}": pl.Int64,
            f"per_share_price_{suffix}": pl.Float64,
        }
        return pl.DataFrame([], schema=output_schema), False

    df_with_prices = df.with_columns(
        (pl.col("value") / pl.col("shares_or_principal_amount")).alias("raw_price")
    )
    median_price = df_with_prices.filter(pl.col("raw_price") > 0)["raw_price"].median()

    requires_multiplication = False
    if median_price is not None and median_price < 2.0:  # Threshold of $2.00
        requires_multiplication = True

    updated_df = (
        df_with_prices.filter(
            pl.col("title_of_class")
            .str.to_uppercase()
            .str.contains(COMMON_STOCK_TITLE_OF_CLASS)
        )
        .with_columns(
            pl.col("issuer_name")
            .str.strip_chars()
            .str.to_uppercase()
            .alias("issuer_name_clean")
        )
        .with_columns(
            pl.when(pl.lit(requires_multiplication))
            .then(pl.col("value") * 1000)
            .otherwise(pl.col("value"))
            .alias("corrected_value")
        )
        .group_by(["cusip", "put_or_call"])
        .agg(
            pl.col("issuer_name_clean").first().alias("issuer_name_clean"),
            pl.col("sic").first().alias("sic"),
            pl.col("shares_or_principal_amount").sum().alias(f"total_shares_{suffix}"),
            pl.col("corrected_value").sum().alias(f"total_value_{suffix}"),
        )
        .with_columns(
            pl.when(pl.col(f"total_shares_{suffix}") > 0)
            .then(pl.col(f"total_value_{suffix}") / pl.col(f"total_shares_{suffix}"))
            .otherwise(pl.lit(0))
            .round(2)
            .alias(f"per_share_price_{suffix}")
        )
    )

    return updated_df, requires_multiplication


@app.get("/analysis/{previous_accession}/{latest_accession}", response_model=dict)
async def compare_holdings(
    previous_accession: str,
    latest_accession: str,
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Compare two holdings by their accession numbers
    """

    try:

        acc_latest = latest_accession.strip()
        acc_prev = previous_accession.strip()

        # Get filing details for the previous and latest filings
        db.execute(FILING_QUERY_WITH_PRIORITY, (acc_prev, acc_prev))
        previous_filing = db.fetchone()

        db.execute(FILING_QUERY_WITH_PRIORITY, (acc_latest, acc_latest))
        latest_filing = db.fetchone()

        # TODO: fetch filings by API (async)

        if not previous_filing:
            raise HTTPException(
                status_code=404, detail=f"Previous filing {acc_prev} not found"
            )
        if not latest_filing:
            raise HTTPException(
                status_code=404, detail=f"Latest filing {acc_latest} not found"
            )

        latest_acc = latest_filing["accession_number"]  # type: ignore
        previous_acc = previous_filing["accession_number"]  # type: ignore

        if previous_filing["cik_number"] != latest_filing["cik_number"]:
            return {"error": "CIK for latest and previous quarters do not match"}

        # Ensure correct ordering based on filing dates
        if previous_filing["period_of_report"] > latest_filing["period_of_report"]:
            previous_filing, latest_filing = latest_filing, previous_filing
            acc_prev, acc_latest = (
                previous_acc,
                latest_acc,
            )
        amendment_used = None
        message_parts = []
        if latest_acc != acc_latest:
            message_parts.append(
                f"The latest filing ({acc_latest}) was replaced by its amendment ({latest_acc}) for the comparison."
            )

        if previous_acc != acc_prev:
            message_parts.append(
                f"The previous filing ({acc_prev}) was replaced by its amendment ({previous_acc}) for the comparison."
            )

        if message_parts:
            amendment_used = " ".join(message_parts)

        api_save_comparison(
            previous_acc,
            latest_acc,
            db,
        )

        # Fetch holdings data from the database
        db.execute(HOLDINGS_QUERY, (acc_prev,))
        previous_holdings_data = db.fetchall()

        db.execute(HOLDINGS_QUERY, (acc_latest,))
        latest_holdings_data = db.fetchall()

        # Convert to Polars DataFrames for efficient analysis
        if not previous_holdings_data:
            previous_df = pl.DataFrame([], schema=HOLDINGS_SCHEMA)
        else:
            previous_df = pl.DataFrame(previous_holdings_data, schema=HOLDINGS_SCHEMA)

        if not latest_holdings_data:
            latest_df = pl.DataFrame([], schema=HOLDINGS_SCHEMA)
        else:
            latest_df = pl.DataFrame(latest_holdings_data, schema=HOLDINGS_SCHEMA)

        # Data cleaning and aggregation + filter ONLY COMMON STOCK
        latest_aggregated, latest_multiplication = _process_filings(latest_df)
        prev_aggregated, prev_multiplication = _process_filings(
            previous_df,
            latest=False,
        )

        # other securities (non-common stock)
        latest_other_securities = latest_df.filter(
            ~pl.col("title_of_class")
            .str.to_uppercase()
            .str.contains(COMMON_STOCK_TITLE_OF_CLASS)
        )

        prev_other_securities = previous_df.filter(
            ~pl.col("title_of_class")
            .str.to_uppercase()
            .str.contains(COMMON_STOCK_TITLE_OF_CLASS)
        )

        # Data cleaning and aggregation for latest other securities
        latest_other_aggregated = (
            latest_other_securities.with_columns(
                pl.col("issuer_name")
                .str.strip_chars()
                .str.to_uppercase()
                .alias("issuer_name_clean")
            )
            .group_by(
                ["cusip", "put_or_call"]
            )  # GROUP BY CUSIP RATHER THAN ISSUER NAME
            .agg(
                # Renaming the column to avoid confusion with common stock shares
                pl.col("issuer_name_clean").first().alias("issuer_name_clean"),
                pl.col("shares_or_principal_amount").sum().alias("total_units_latest"),
                pl.col("value").sum().alias("total_value_latest"),
            )
        )

        # Data cleaning and aggregation for previous other securities
        prev_other_aggregated = (
            prev_other_securities.with_columns(
                pl.col("issuer_name")
                .str.strip_chars()
                .str.to_uppercase()
                .alias("issuer_name_clean")
            )
            .group_by(["cusip", "put_or_call"])
            .agg(
                # Renaming the column to avoid confusion with common stock shares
                pl.col("issuer_name_clean").first().alias("issuer_name_clean"),
                pl.col("shares_or_principal_amount").sum().alias("total_units_prev"),
                pl.col("value").sum().alias("total_value_prev"),
            )
        )

        # join
        merged_df = latest_aggregated.join(
            prev_aggregated, on="cusip", how="full", suffix="_prev"
        )

        #### SECTOR ANALYSIS ####
        sector_changes = (
            merged_df.with_columns(
                # Coalesce SIC codes, preferring the latest one
                pl.coalesce(pl.col("sic"), pl.col("sic_prev")).alias("final_sic")
            )
            .filter(pl.col("final_sic").is_not_null())  # Ensure SIC code is present
            .with_columns(
                pl.col("final_sic")
                .map_elements(get_sic_division, return_dtype=pl.Utf8)
                .alias("sector"),
            )
            .group_by("sector")
            .agg(
                pl.col("total_value_latest")
                .fill_null(0)
                .sum()
                .alias("latest_sector_total"),
                pl.col("total_value_prev")
                .fill_null(0)
                .sum()
                .alias("prev_sector_total"),
            )
            .with_columns(
                # Calculate percentage change
                pl.when(pl.col("prev_sector_total") > 0)
                .then(
                    (
                        (pl.col("latest_sector_total") - pl.col("prev_sector_total"))
                        / pl.col("prev_sector_total")
                    )
                    * 100
                )
                # If prev total was 0, change is infinite/new; set to None
                .otherwise(None)
                .round(2)
                .alias("percent_change")
            )
            .filter(pl.col("percent_change").is_not_null())
        )

        increased_sectors = sector_changes.filter(pl.col("percent_change") > 0).sort(
            "percent_change", descending=True
        )

        decreased_sectors = sector_changes.filter(pl.col("percent_change") < 0).sort(
            "percent_change", descending=False
        )

        ### SECTOR ANALYSIS END ###

        merged_other_df = latest_other_aggregated.join(
            prev_other_aggregated, on="cusip", how="full", suffix="_prev"
        )

        # if previously no shares, now has shares -> new holding
        new_holdings = merged_df.filter(pl.col("total_shares_prev").is_null()).select(
            "issuer_name_clean",
            "total_shares_latest",
            "total_value_latest",
            "per_share_price_latest",
            "put_or_call",
            "cusip",
        )
        closed_positions = merged_df.filter(
            pl.col("total_shares_latest").is_null()
        ).select(
            "issuer_name_clean_prev",
            "total_shares_prev",
            "total_value_prev",
            "per_share_price_prev",
            "put_or_call",
            "cusip_prev",
        )
        new_other_holdings = merged_other_df.filter(
            pl.col("total_units_prev").is_null()
        ).select(
            "issuer_name_clean",
            "total_units_latest",
            "total_value_latest",
            "put_or_call",
        )
        closed_other_positions = merged_other_df.filter(
            pl.col("total_units_latest").is_null()
        ).select(
            "issuer_name_clean_prev",
            "total_units_prev",
            "total_value_prev",
            "put_or_call",
        )

        # Common holdings with changes (increase or decreases)
        common_holdings = (
            merged_df.filter(
                pl.col("total_shares_prev").is_not_null()
                & pl.col("total_shares_latest").is_not_null()
            )
            .with_columns(
                [
                    (pl.col("total_shares_latest") - pl.col("total_shares_prev")).alias(
                        "change_in_share"
                    ),
                    (
                        (pl.col("total_shares_latest") - pl.col("total_shares_prev"))
                        / pl.col("total_shares_prev")
                        * 100
                    )
                    .round(2)
                    .alias("percent_change"),
                ]
            )
            .with_columns(
                pl.when(pl.col("percent_change").is_nan())
                .then(None)
                .when(pl.col("percent_change").is_infinite())
                .then(None)
                .otherwise(pl.col("percent_change"))
                .alias("percent_change")
            )
            .select(
                "issuer_name_clean",
                "total_shares_prev",
                "total_value_prev",
                "per_share_price_prev",
                "total_shares_latest",
                "total_value_latest",
                "per_share_price_latest",
                "change_in_share",
                "percent_change",
                "put_or_call",
                "cusip",
            )
        )

        # Increases
        increased_holdings = common_holdings.filter(pl.col("change_in_share") > 0)
        decreased_holdings = common_holdings.filter(pl.col("change_in_share") < 0)
        unchanged_holdings = common_holdings.filter(pl.col("change_in_share") == 0)

        # other securities with changes (increase or decreases)
        common_other_holdings = (
            merged_other_df.filter(
                pl.col("total_units_prev").is_not_null()
                & pl.col("total_units_latest").is_not_null()
            )
            .with_columns(
                [
                    (pl.col("total_units_latest") - pl.col("total_units_prev")).alias(
                        "change_in_units"
                    ),
                    (
                        (pl.col("total_units_latest") - pl.col("total_units_prev"))
                        / pl.col("total_units_prev")
                        * 100
                    )
                    .round(2)
                    .alias("percent_change"),
                ]
            )
            .with_columns(
                pl.when(pl.col("percent_change").is_nan())
                .then(None)
                .when(pl.col("percent_change").is_infinite())
                .then(None)
                .otherwise(pl.col("percent_change"))
                .alias("percent_change")
            )
            .select(
                "issuer_name_clean",
                "total_units_prev",
                "total_value_prev",
                "total_units_latest",
                "total_value_latest",
                "change_in_units",
                "percent_change",
                "put_or_call",
            )
        )

        # Increases
        increased_other_holdings = common_other_holdings.filter(
            pl.col("change_in_units") > 0
        )
        decreased_other_holdings = common_other_holdings.filter(
            pl.col("change_in_units") < 0
        )
        unchanged_other_holdings = common_other_holdings.filter(
            pl.col("change_in_units") == 0
        )

        top_5_holdings_other_by_value = latest_other_aggregated.sort(
            by="total_value_latest", descending=True
        ).head(5)
        top_5_holdings_by_value = latest_aggregated.sort(
            by="total_value_latest", descending=True
        ).head(5)
        top_5_new_common = new_holdings.sort(
            by="total_value_latest", descending=True
        ).head(5)
        top_5_closed_common = closed_positions.sort(
            by="total_value_prev", descending=True
        ).head(5)
        top_5_increased_common = increased_holdings.sort(
            by="percent_change", descending=True
        ).head(5)
        top_5_decreased_common = decreased_holdings.sort(by="percent_change").head(5)

        # Prepare response data
        response_data = {
            "metadata": {
                "cik": latest_filing.get("cik_number"),
                "company_name": latest_filing.get("company_name"),
                "ai_summary": "Not Available",
                "amendment_used": amendment_used,
                "latest_filing": {
                    "accession_number": latest_filing.get("accession_number"),
                    "filing_date": (
                        latest_filing.get("filing_date").isoformat()
                        if latest_filing.get("filing_date")
                        else None
                    ),
                    "period_of_report": latest_filing.get("period_of_report"),
                    "form_type": latest_filing.get("form_type"),
                    "user_input": acc_latest,
                    "filing_directory": latest_filing.get("filing_directory"),
                    "multiplication_applied": latest_multiplication,
                },
                "previous_filing": {
                    "accession_number": previous_filing.get("accession_number"),
                    "filing_date": (
                        previous_filing.get("filing_date").isoformat()
                        if previous_filing.get("filing_date")
                        else None
                    ),
                    "period_of_report": previous_filing.get("period_of_report"),
                    "form_type": previous_filing.get("form_type"),
                    "user_input": acc_prev,
                    "filing_directory": previous_filing.get("filing_directory"),
                    "multiplication_applied": prev_multiplication,
                },
                "summary": {
                    "total_companies_latest": latest_aggregated.height,
                    "total_companies_previous": prev_aggregated.height,
                    "new_holdings_count": new_holdings.height,
                    "closed_positions_count": closed_positions.height,
                    "increased_holdings_count": increased_holdings.height,
                    "decreased_holdings_count": decreased_holdings.height,
                    "unchanged_holdings_count": unchanged_holdings.height,
                    "sector_changes": {
                        "increased_by_sector": increased_sectors.to_dicts(),
                        "decreased_by_sector": decreased_sectors.to_dicts(),
                    },
                },
            },
            "holdings": {
                "top_holdings_by_value": top_5_holdings_by_value.to_dicts(),
                "top_other_securities_by_value": top_5_holdings_other_by_value.to_dicts(),
                "new_holdings": {
                    "top_5": top_5_new_common.to_dicts(),
                    "common_stock": _df_to_dict_list(new_holdings),
                    "other_securities": _df_to_dict_list(new_other_holdings),
                },
                "closed_positions": {
                    "top_5": top_5_closed_common.to_dicts(),
                    "common_stock": _df_to_dict_list(closed_positions),
                    "other_securities": _df_to_dict_list(closed_other_positions),
                },
                "increased_holdings": {
                    "top_5": top_5_increased_common.to_dicts(),
                    "common_stock": _df_to_dict_list(increased_holdings),
                    "other_securities": _df_to_dict_list(increased_other_holdings),
                },
                "decreased_holdings": {
                    "top_5": top_5_decreased_common.to_dicts(),
                    "common_stock": _df_to_dict_list(decreased_holdings),
                    "other_securities": _df_to_dict_list(decreased_other_holdings),
                },
                "common_holdings": {
                    "common_stock": _df_to_dict_list(unchanged_holdings),
                    "other_securities": _df_to_dict_list(unchanged_other_holdings),
                },
            },
        }

        return response_data

    except HTTPException as e:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {e}")


@app.get("/comparisons", response_model=list)
def get_recent_comparisons(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    try:
        query = """
            SELECT accession_number_1, accession_number_2, created_at
            FROM recent_comparisons
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """
        db.execute(query, (limit, offset))
        comparisons = db.fetchall()
        return comparisons
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ComparisonRequest(BaseModel):
    acc_num_1: str
    acc_num_2: str


@app.post("/comparisons")
def save_comparison(
    request: ComparisonRequest,
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    try:
        query = """
            INSERT INTO recent_comparisons (accession_number_1, accession_number_2)
            VALUES (%s, %s)
        """
        db.execute(query, (request.acc_num_1, request.acc_num_2))
        db.connection.commit()

        return {"message": "Comparison saved successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def api_save_comparison(accession_1: str, accession_2: str, db):
    try:
        query = """
            INSERT INTO recent_comparisons (accession_number_1, accession_number_2)
            VALUES (%s, %s)
        """
        db.execute(query, (accession_1, accession_2))
        db.connection.commit()

        return {"message": "Comparison saved successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/company/{cik}/compare/latest")
async def compare_latest_filings(
    cik: str,
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Automatically compares the latest two quarterly filings for a given company CIK.
    """
    try:
        # Step 1: Find the latest two filings for the given CIK
        filing_query = """
            SELECT
                accession_number,
                period_of_report
            FROM (
                SELECT
                    f.accession_number,
                    f.period_of_report,
                    ROW_NUMBER() OVER(PARTITION BY f.period_of_report ORDER BY f.filing_date DESC, f.accession_number DESC) as rn
                FROM filings f
                INNER JOIN companies c ON f.company_id = c.company_id
                WHERE c.cik_number = %s AND f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
            ) AS ranked_filings
            WHERE rn = 1
            ORDER BY period_of_report desc
            LIMIT 2;
        """
        db.execute(filing_query, (cik,))
        filings = db.fetchall()

        if len(filings) < 2:
            raise HTTPException(
                status_code=404,
                detail=f"Not enough filings found for CIK {cik} to perform a comparison.",
            )

        latest_filing = filings[0]["accession_number"]
        previous_filing = filings[1]["accession_number"]

        print(f"Comparing filings {previous_filing} and {latest_filing} for CIK {cik}")

        # Step 2: Use the existing compare_holdings logic to perform the comparison
        # You'll need to call the function directly here
        return await compare_holdings(
            previous_accession=previous_filing,
            latest_accession=latest_filing,
            db=db,
        )

    except HTTPException:
        raise  # Re-raise HTTPException to be handled by FastAPI
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"An unexpected error occurred: {str(e)}"
        )


class SignificantHolding(BaseModel):
    """Represents a single significant holding for the story."""

    issuer_name: str
    cusip: str | None = None
    shares_or_principal_amount: int
    value: int
    # The 'change_type' can be 'new', 'increase', 'decrease', etc.
    change_type: str
    price_per_share: float | None = None


class HoldingChange(BaseModel):
    issuer_name: str
    cusip: Optional[str] = None
    shares_or_principal_amount: int  # The new, current amount of shares
    change_in_share: int
    percent_change: Optional[float] = (
        None  # Use float for percentage, optional for safety
    )
    price_per_share: Optional[float] = None
    price_per_unit: Optional[float] = None
    change_type: str


class StorySummary(BaseModel):
    """A summary of a single filing's story for a list view."""

    cik: str
    aum: int | None = None
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


FILING_QUERY_V2 = """
WITH RankedFilings AS (
    SELECT
        f.filing_id,
        f.company_id,
        f.accession_number,
        f.period_of_report,
        f.filing_date,
        f.created_at,
        c.company_name,
        c.cik_number,
        c.aum,
        ROW_NUMBER() OVER(PARTITION BY f.company_id ORDER BY f.filing_date DESC, f.created_at DESC) as rn
    FROM
        filings f
    JOIN
        companies c ON f.company_id = c.company_id
    WHERE
        f.form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
),
LatestFilings AS (
    SELECT * FROM RankedFilings WHERE rn = 1
)
SELECT
    lf.*,
    pf.filing_id AS previous_filing_id,
    pf.accession_number AS previous_accession_number
FROM LatestFilings lf
LEFT JOIN LATERAL (
    SELECT filing_id, accession_number
    FROM filings
    WHERE company_id = lf.company_id
        AND period_of_report < lf.period_of_report
        AND form_type IN ('13F-HR', '13F-HR/A', '13F-HR/A/A')
    ORDER BY period_of_report DESC, filing_date DESC
    LIMIT 1
) pf ON true
ORDER BY lf.filing_date DESC, lf.created_at DESC
LIMIT %(limit)s OFFSET %(offset)s;
"""

FILTER_CANDIDATES_QUERY_V2 = """
    SELECT DISTINCT h.filing_id
    FROM holdings h
    JOIN title_of_class_table tc ON h.title_of_class = tc.id
    WHERE
        h.filing_id = ANY(%(candidate_filing_ids)s)
      AND tc.name ~* %(common_stock_pattern)s;
"""

MODIFIED_OPTIMISED_STORIES_QUERY = """
    WITH FilingsWithPrevious AS (
        SELECT * FROM (VALUES %s) AS t (
            filing_id, company_id, accession_number, period_of_report, filing_date,
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
            (tc.name ~* %(common_stock_pattern)s) as is_common_stock
        FROM holdings h
        JOIN issuers i ON h.issuer_id = i.issuer_id
        JOIN title_of_class_table tc ON h.title_of_class = tc.id
        WHERE h.filing_id = ANY(%(filing_ids_to_process)s)
        GROUP BY h.filing_id, i.cusip, is_common_stock
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
        ) hc ON true -- hc = holdings comparison
        WHERE fwp.previous_filing_id IS NOT NULL
    ),
    RankedChanges AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY filing_id, is_common_stock, change_type
                ORDER BY
                    -- For new/closed positions, rank by the position's value.
                    CASE WHEN change_type IN ('new', 'closed') THEN COALESCE(current_value, previous_value) END DESC,
                    -- For increased/decreased, rank by the absolute percentage change.
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
            CASE
                WHEN (rc.current_value::numeric / rc.current_shares) < 1.0 THEN (rc.current_value::numeric * 1000) / rc.current_shares
                ELSE (rc.current_value::numeric) / rc.current_shares
            END
        ELSE 0 END) AS top_new_price,
        -- Common Stock - Closed
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_closed_issuer,
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_closed_cusip,
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 THEN rc.previous_shares END) AS top_closed_shares,
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 THEN rc.previous_value END) AS top_closed_value,
        MAX(CASE WHEN rc.change_type = 'closed' AND rc.is_common_stock AND rc.rn = 1 AND rc.previous_shares > 0 THEN
            CASE
                WHEN (rc.previous_value::numeric / rc.previous_shares) < 1.0 THEN (rc.previous_value::numeric * 1000) / rc.previous_shares
                ELSE (rc.previous_value::numeric) / rc.previous_shares
            END
        ELSE 0 END) AS top_closed_price,
        -- Common Stock - Increased
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_increased_issuer,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_increased_cusip,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_increased_shares,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.change_in_share END) AS top_increased_change_in_share,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.percent_change END) AS top_increased_percent_change,
        MAX(CASE WHEN rc.change_type = 'increased' AND rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            CASE
                WHEN (rc.current_value::numeric / rc.current_shares) < 1.0 THEN (rc.current_value::numeric * 1000) / rc.current_shares
                ELSE (rc.current_value::numeric) / rc.current_shares
            END
        ELSE 0 END) AS top_increased_price,
        -- Common Stock - Decreased
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_decreased_issuer,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_decreased_cusip,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_decreased_shares,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.change_in_share END) AS top_decreased_change_in_share,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 THEN rc.percent_change END) AS top_decreased_percent_change,
        MAX(CASE WHEN rc.change_type = 'decreased' AND rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            CASE
                WHEN (rc.current_value::numeric / rc.current_shares) < 1.0 THEN (rc.current_value::numeric * 1000) / rc.current_shares
                ELSE (rc.current_value::numeric) / rc.current_shares
            END
        ELSE 0 END) AS top_decreased_price,
        -- Other Securities - New
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_new_other_issuer,
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_new_other_cusip,
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_new_other_shares,
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.current_value END) AS top_new_other_value,
        MAX(CASE WHEN rc.change_type = 'new' AND NOT rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            CASE
                WHEN (rc.current_value::numeric / rc.current_shares) < 1.0 THEN (rc.current_value::numeric * 1000) / rc.current_shares
                ELSE (rc.current_value::numeric) / rc.current_shares
            END
        ELSE 0 END) AS top_new_other_price,  
        -- Other Securities - Closed
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_closed_other_issuer,
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_closed_other_cusip,
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.previous_shares END) AS top_closed_other_shares,
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.previous_value END) AS top_closed_other_value,
        MAX(CASE WHEN rc.change_type = 'closed' AND NOT rc.is_common_stock AND rc.rn = 1 AND rc.previous_shares > 0 THEN
            CASE
                WHEN (rc.previous_value::numeric / rc.previous_shares) < 1.0 THEN (rc.previous_value::numeric * 1000) / rc.previous_shares
                ELSE (rc.previous_value::numeric) / rc.previous_shares
            END
        ELSE 0 END) AS top_closed_other_price,
        -- Other Securities - Increased
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_increased_other_issuer,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_increased_other_cusip,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_increased_other_shares,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.change_in_share END) AS top_increased_other_change_in_share,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.percent_change END) AS top_increased_other_percent_change,
        MAX(CASE WHEN rc.change_type = 'increased' AND NOT rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            CASE
                WHEN (rc.current_value::numeric / rc.current_shares) < 1.0 THEN (rc.current_value::numeric * 1000) / rc.current_shares
                ELSE (rc.current_value::numeric) / rc.current_shares
            END
        ELSE 0 END) AS top_increased_other_price,
        -- Other Securities - Decreased
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.issuer_name END) AS top_decreased_other_issuer,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.cusip END) AS top_decreased_other_cusip,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.current_shares END) AS top_decreased_other_shares,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.change_in_share END) AS top_decreased_other_change_in_share,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 THEN rc.percent_change END) AS top_decreased_other_percent_change,
        MAX(CASE WHEN rc.change_type = 'decreased' AND NOT rc.is_common_stock AND rc.rn = 1 AND rc.current_shares > 0 THEN
            CASE
                WHEN (rc.current_value::numeric / rc.current_shares) < 1.0 THEN (rc.current_value::numeric * 1000) / rc.current_shares
                ELSE (rc.current_value::numeric) / rc.current_shares
            END
        ELSE 0 END) AS top_decreased_other_price
    FROM
        FilingsWithPrevious fwp
    LEFT JOIN
        RankedChanges rc ON fwp.filing_id = rc.filing_id
    GROUP BY
        fwp.filing_id, fwp.cik_number, fwp.aum, fwp.accession_number, fwp.previous_accession_number,
        fwp.company_name, fwp.period_of_report, fwp.filing_date
    ORDER BY
        fwp.filing_date DESC, fwp.accession_number DESC;
"""


@app.get("/stories/latest/v2", response_model=LatestStoriesResponse)
async def get_latest_stories_v2(
    limit: int = Query(20, description="Number of stories to return", ge=1, le=50),
    offset: int = Query(
        0, description="Number of stories to skip for pagination", ge=0
    ),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieves a list of the latest filings, each with a summary of its most
    significant new holding. (Now optimized!)
    """
    try:
        # Get filings
        db.execute(FILING_QUERY_V2, {"limit": limit + 1, "offset": offset})
        print("Executed FILING_QUERY_V2")
        candidate_filings = db.fetchall()

        has_next_page = len(candidate_filings) > limit
        candidates_to_process = candidate_filings[:limit]

        if not candidates_to_process:
            return LatestStoriesResponse(stories=[])

        candidate_filing_ids = [f["filing_id"] for f in candidates_to_process]
        params = {
            "candidate_filing_ids": candidate_filing_ids,
            "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
        }
        # Filter filings
        db.execute(FILTER_CANDIDATES_QUERY_V2, params)
        print("Executed FILTER_CANDIDATES_QUERY_V2")
        valid_filing_ids = {row["filing_id"] for row in db.fetchall()}

        # Filter our candidate list in Python
        valid_filings = [
            f for f in candidate_filings if f["filing_id"] in valid_filing_ids
        ]

        if not valid_filings:
            return LatestStoriesResponse(stories=[])

        filing_ids_to_process = set()
        filing_data_tuples = []

        for f in valid_filings:
            filing_ids_to_process.add(f["filing_id"])
            if f["previous_filing_id"]:
                filing_ids_to_process.add(f["previous_filing_id"])

            # Prepare data for the VALUES clause
            filing_data_tuples.append(
                (
                    f["filing_id"],
                    f["company_id"],
                    f["accession_number"],
                    f["period_of_report"],
                    f["filing_date"],
                    f["company_name"],
                    f["cik_number"],
                    f["aum"],
                    f["previous_filing_id"],
                    f["previous_accession_number"],
                )
            )

        values_string_list = []
        for t in filing_data_tuples:
            # db.mogrify() safely formats a single tuple
            values_string_list.append(
                db.mogrify("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", t).decode(
                    "utf-8"
                )
            )

        values_string = ",\n".join(values_string_list)

        # 2. Modify the query template to *remove* the %s and insert our safe string.
        # We use .format() here because we have already safely escaped the data.
        final_query_template = MODIFIED_OPTIMISED_STORIES_QUERY.replace(
            "%s", values_string
        )

        # 3. Create the dictionary for the *other* (named) parameters.
        final_query_params = {
            "valid_filing_ids": list(valid_filing_ids),
            "filing_ids_to_process": list(filing_ids_to_process),
            "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
        }
        db.execute(final_query_template, final_query_params)
        print("Executing final optimized stories query")

        # db.execute(query_with_values, final_query_params)
        results = db.fetchall()

        story_summaries = []
        for row in results:
            # Common Stock Holdings
            top_new = (
                SignificantHolding(
                    issuer_name=row["top_new_issuer"],
                    cusip=row["top_new_cusip"],
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
                    shares_or_principal_amount=row["top_decreased_shares"],
                    change_in_share=row["top_decreased_change_in_share"],
                    percent_change=row["top_decreased_percent_change"],
                    price_per_share=row["top_decreased_price"],
                    change_type="decreased",
                )
                if row["top_decreased_issuer"]
                else None
            )

            # Other Securities Holdings (assuming similar aliases like 'top_new_other_issuer')
            top_new_other = (
                SignificantHolding(
                    issuer_name=row["top_new_other_issuer"],
                    cusip=row["top_new_other_cusip"],
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
                    # Common Stock
                    top_new_position=top_new,
                    top_closed_position=top_closed,
                    top_increased_position=top_increased,
                    top_decreased_position=top_decreased,
                    # Other Securities
                    top_new_other_securities=top_new_other,
                    top_closed_other_securities=top_closed_other,
                    top_increased_other_securities=top_increased_other,
                    top_decreased_other_securities=top_decreased_other,
                )
            )

        return LatestStoriesResponse(
            stories=story_summaries, has_next_page=has_next_page
        )

    except Exception as e:
        # It's good practice to log the full exception for debugging
        # import logging
        # logging.exception("Error fetching latest stories")
        raise HTTPException(
            status_code=500, detail=f"An internal error occurred: {str(e)}"
        )


LATEST_ACTIVITY_QUERY_V3 = """
    WITH FilingsWithPrevious AS (
        SELECT * FROM (VALUES %s) AS t (
            filing_id, company_id, accession_number, period_of_report, filing_date,
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
            (tc.name ~* %(common_stock_pattern)s) as is_common_stock
        FROM holdings h
        JOIN issuers i ON h.issuer_id = i.issuer_id
        JOIN title_of_class_table tc ON h.title_of_class = tc.id
        WHERE h.filing_id = ANY(%(filing_ids_to_process)s)
        GROUP BY h.filing_id, i.cusip, is_common_stock
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
            FilingsWithPrevious fwp
        LEFT JOIN
            (SELECT * FROM AggregatedHoldings) AS curr ON fwp.filing_id = curr.filing_id
        FULL OUTER JOIN
            (SELECT * FROM AggregatedHoldings) AS prev
            ON fwp.previous_filing_id = prev.filing_id AND curr.cusip = prev.cusip AND curr.is_common_stock = prev.is_common_stock
        WHERE
            fwp.previous_filing_id IS NOT NULL
            AND (curr.cusip IS NOT NULL OR prev.cusip IS NOT NULL) -- Ensure there's a holding on either side
    )
    SELECT
        hc.cik_number AS cik,
        hc.company_name,
        hc.accession_number AS latest_accession_number,
        hc.previous_accession_number,
        hc.period_of_report AS reporting_period,
        hc.filing_date,
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
                WHEN hc.current_shares > 0 THEN
                    CASE
                        -- Only apply 1000x logic IF it is common stock AND price is < 1.0
                        WHEN hc.is_common_stock AND (hc.current_value::numeric / hc.current_shares) < 1.0 
                        THEN (hc.current_value::numeric * 1000) / hc.current_shares
                        
                        -- Otherwise (not common stock OR price is >= 1.0), calculate normally
                        ELSE (hc.current_value::numeric) / hc.current_shares
                    END
                ELSE 0
            END, 2
        ) AS current_price_per_share,
        ROUND(
            CASE
                WHEN hc.previous_shares > 0 THEN
                    CASE
                        -- Only apply 1000x logic IF it is common stock AND price is < 1.0
                        WHEN hc.is_common_stock AND (hc.previous_value::numeric / hc.previous_shares) < 1.0 
                        THEN (hc.previous_value::numeric * 1000) / hc.previous_shares
                        
                        -- Otherwise (not common stock OR price is >= 1.0), calculate normally
                        ELSE (hc.previous_value::numeric) / hc.previous_shares
                    END
                ELSE 0
            END, 2
        ) AS previous_price_per_share
    FROM
        HoldingsComparison hc
    WHERE
        hc.change_type IN ('new', 'closed', 'increased', 'decreased')
    ORDER BY
        cik DESC; 
"""


@app.get("/activity/latest/v3", response_model=LatestActivityResponse)
async def get_latest_activity_v3(
    limit: int = Query(
        3, description="Number of *companies* to fetch stories for", ge=1, le=50
    ),
    offset: int = Query(
        0, description="Number of *companies* to skip for pagination", ge=0
    ),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Retrieves a flat list of all significant holding changes (headlines)
    from the latest batch of company filings.
    """
    try:
        # Step 1: Get candidate companies (paginated)
        db.execute(FILING_QUERY_V2, {"limit": limit + 1, "offset": offset})
        candidate_filings = db.fetchall()

        has_next_page = len(candidate_filings) > limit
        candidates_to_process = candidate_filings[:limit]

        if not candidates_to_process:
            return LatestActivityResponse(activities=[])

        # Step 2: Filter candidates to only those with common stock
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

        # Step 3: Prepare the VALUES list and parameters for the main query
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
                    f["company_name"],
                    f["cik_number"],
                    f["aum"],
                    f["previous_filing_id"],
                    f["previous_accession_number"],
                )
            )

        # Safely create the VALUES string
        values_string_list = []
        for t in filing_data_tuples:
            values_string_list.append(
                db.mogrify("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", t).decode(
                    "utf-8"
                )
            )
        values_string = ",\n".join(values_string_list)

        # Prepare the final query
        final_query_template = LATEST_ACTIVITY_QUERY_V3.replace("%s", values_string)
        final_query_params = {
            "valid_filing_ids": list(valid_filing_ids),
            "filing_ids_to_process": list(filing_ids_to_process),
            "common_stock_pattern": COMMON_STOCK_TITLE_OF_CLASS,
        }

        # Step 4: Execute the query and serialize the results
        db.execute(final_query_template, final_query_params)
        results = db.fetchall()

        # Serialize into our new HoldingActivity model
        activities = [HoldingActivity(**row) for row in results]

        return LatestActivityResponse(
            activities=activities, has_next_page=has_next_page
        )

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"An internal error occurred: {str(e)}"
        )


@app.get("/api/search/companies", response_model=List[Dict[str, str]])
def search_companies(
    q: str = Query(
        ..., min_length=2, description="Search term for company name or CIK"
    ),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Search for companies by name or CIK for autocomplete.
    """
    if len(q) < 2:
        return []

    search_name = f"%{q}%"
    search_cik = f"{q}%"  # CIKs are usually searched from the start

    query = """
        SELECT
            company_name,
            cik_number
        FROM companies
        WHERE
            company_name ILIKE %s
            OR cik_number::text ILIKE %s
        LIMIT 10;
    """
    try:
        db.execute(query, (search_name, search_cik))
        results = db.fetchall()

        companies = [
            {"name": row["company_name"], "cik": str(row["cik_number"])}
            for row in results
        ]
        return companies
    except Exception as e:
        raise HTTPException(status_code=500, detail="Error during company search")


def fetch_clean_holdings_df(
    cursor,
    target_cusip: str,
    start_date: Optional[dt.date] = None,
) -> pd.DataFrame:
    """
    Fetches historical holdings for a CUSIP, filtering for the
    latest filing per quarter to handle amendments.
    """
    base_query = """
        WITH latest_filings_filter AS (
            SELECT filing_id, company_id, period_of_report
            FROM (
                SELECT 
                    filing_id, company_id, period_of_report,
                    ROW_NUMBER() OVER (
                        PARTITION BY company_id, period_of_report 
                        ORDER BY filing_date DESC, accession_number DESC
                    ) as rn
                FROM filings
            ) sub
            WHERE rn = 1
        )
        SELECT 
            c.cik_number,
            lf.period_of_report as period,
            SUM(h.shares_or_principal_amount) as shares
        FROM holdings h
        JOIN latest_filings_filter lf ON h.filing_id = lf.filing_id
        JOIN companies c ON lf.company_id = c.company_id
        JOIN issuers i ON h.issuer_id = i.issuer_id
        LEFT JOIN put_or_call_table poc ON h.put_or_call = poc.id
        WHERE 
            i.cusip = %s
            AND (poc.name = 'NONE' OR poc.name IS NULL)
        """

    # Dynamic SQL parameter construction
    params = [target_cusip]

    if start_date:
        base_query += " AND lf.period_of_report >= %s"
        params.append(start_date)

    # Add Group By and Order By at the end
    base_query += """
        GROUP BY c.cik_number, lf.period_of_report
        ORDER BY lf.period_of_report ASC
    """

    # Execute query
    cursor.execute(base_query, tuple(params))
    results = cursor.fetchall()

    if not results:
        return pd.DataFrame()

    return pd.DataFrame(results)


# @app.get("/api/v1/flow/{cusip}", response_model=FlowResponse)
def get_stock_flow(
    cusip: str,
    years: Optional[int] = Query(
        None, description="Limit analysis to the past X years"
    ),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Calculates the net buying/selling flow for a specific CUSIP.
    Aggregated over period of report (quarterly)
    """

    try:
        # 1. Get Clean Data directly using CUSIP
        # Note: We strip whitespace to be safe
        clean_cusip = cusip.strip()

        start_date = None
        if years:
            # Get today's date minus X years
            start_date = dt.date.today() - dt.timedelta(days=365 * years)

        df = fetch_clean_holdings_df(db, clean_cusip, start_date=start_date)

        # 2. Handle Empty Case (No data found for this CUSIP)
        if df.empty:
            return {"cusip": clean_cusip, "history": []}

        # 3. The "Pandas Magic" (Pivot -> Fill -> Diff)
        df["period"] = pd.to_datetime(df["period"])

        # Pivot: Rows=Date, Cols=Fund CIK
        pivot_df = df.pivot(index="period", columns="cik_number", values="shares")

        # CRITICAL: Fill missing with 0 so "Sold Out" and "New Buy" are captured
        pivot_df = pivot_df.fillna(0)

        # Calculate Quarter-over-Quarter change
        change_df = pivot_df.diff()

        # 4. Aggregate Totals
        analysis_df = pd.DataFrame(index=change_df.index)
        analysis_df["gross_buying"] = change_df[change_df > 0].sum(axis=1)
        analysis_df["gross_selling"] = change_df[change_df < 0].sum(axis=1).abs()
        analysis_df["net_change"] = (
            analysis_df["gross_buying"] - analysis_df["gross_selling"]
        )

        # Drop the first row (invalid diff)
        analysis_df = analysis_df.iloc[1:]

        # 5. Format for JSON
        history = []
        for date, row in analysis_df.iterrows():
            history.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "gross_buying": float(row["gross_buying"]),
                    "gross_selling": float(row["gross_selling"]),
                    "net_change": float(row["net_change"]),
                }
            )

        return {"cusip": clean_cusip, "history": history}

    except Exception as e:
        print(f"Error processing CUSIP {cusip}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.get("/api/v1/flow/daily/{identifier}", response_model=DailyFlowResponse)
def get_daily_stock_flow(
    identifier: str,
    days: int = Query(1, description="Number of days to look back", ge=1, le=365),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Aggregates buying/selling flow based on the FILING DATE.
    This shows 'what was reported today' rather than 'what happened in Q3'.
    """
    # Clean the input and make uppercase to match our dictionary keys
    identifier_clean = identifier.strip().upper()

    ticker = None
    if identifier_clean in TICKER_TO_CUSIP:
        ticker = identifier_clean
        clean_cusip = TICKER_TO_CUSIP[identifier_clean]
    else:
        clean_cusip = identifier_clean
        CUSIP_TO_TICKER = {v: k for k, v in TICKER_TO_CUSIP.items()}
        if clean_cusip in CUSIP_TO_TICKER:
            ticker = CUSIP_TO_TICKER[clean_cusip]

    print(
        f"Received request for identifier: {identifier}, resolved Ticker: {ticker}, CUSIP: {clean_cusip}"
    )

    start_date = dt.date.today() - dt.timedelta(days=days)

    query = """
    WITH RelevantFilings AS (
        -- 1. Identify all 13F filings submitted in the requested date range
        SELECT
            f.filing_id,
            f.company_id,
            f.filing_date,
            f.period_of_report
        FROM filings f
        WHERE f.filing_date >= %s
          AND f.form_type IN ('13F-HR', '13F-HR/A')
    ),
    PreviousFilings AS (
        -- 2. For each relevant filing, find the fund's immediately preceding filing
        --    This serves as the 'baseline' to calculate the change.
        SELECT
            rf.filing_id AS current_filing_id,
            (
                SELECT f2.filing_id
                FROM filings f2
                WHERE f2.company_id = rf.company_id
                  AND f2.period_of_report < rf.period_of_report
                  AND f2.form_type IN ('13F-HR', '13F-HR/A')
                ORDER BY f2.period_of_report DESC, f2.filing_date DESC, f2.accession_number DESC
                LIMIT 1
            ) AS previous_filing_id
        FROM RelevantFilings rf
    ),
    HoldingsComparison AS (
        -- 3. Calculate the delta for the specific CUSIP between Current and Previous
        SELECT
            rf.filing_date,
            COALESCE(h_curr.shares_or_principal_amount, 0) as current_shares,
            COALESCE(h_prev.shares_or_principal_amount, 0) as previous_shares
        FROM RelevantFilings rf
        JOIN PreviousFilings pf ON rf.filing_id = pf.current_filing_id
        -- Join Current Holdings (Left Join to capture 'Sold Out' if needed, though filtered below)
        LEFT JOIN (
            SELECT h.filing_id, h.shares_or_principal_amount
            FROM holdings h
            JOIN issuers i ON h.issuer_id = i.issuer_id
            WHERE i.cusip = %s 
        ) h_curr ON rf.filing_id = h_curr.filing_id
        -- Join Previous Holdings
        LEFT JOIN (
            SELECT h.filing_id, h.shares_or_principal_amount
            FROM holdings h
            JOIN issuers i ON h.issuer_id = i.issuer_id
            WHERE i.cusip = %s
        ) h_prev ON pf.previous_filing_id = h_prev.filing_id
        -- Filter: Only keep rows where the stock exists in at least one of the filings
        -- This captures Buys (exists in Curr) and Sells (exists in Prev but not Curr)
        WHERE h_curr.shares_or_principal_amount IS NOT NULL
           OR h_prev.shares_or_principal_amount IS NOT NULL
    )
    -- 4. Group by Filing Date to get the daily aggregate
    SELECT
        filing_date,
        SUM(CASE WHEN current_shares > previous_shares THEN (current_shares - previous_shares) ELSE 0 END) as gross_buying,
        SUM(CASE WHEN current_shares < previous_shares THEN ABS(current_shares - previous_shares) ELSE 0 END) as gross_selling,
        SUM(current_shares - previous_shares) as net_change
    FROM HoldingsComparison
    GROUP BY filing_date
    ORDER BY filing_date DESC;
    """

    try:
        # Pass params: start_date, then cusip twice (for current and prev join)
        db.execute(query, (start_date, clean_cusip, clean_cusip))
        print(
            f"Executed daily flow query for CUSIP {clean_cusip} starting from {start_date}"
        )
        results = db.fetchall()

        daily_data = [
            DailyFlowEntry(
                date=row["filing_date"],
                gross_buying=float(row["gross_buying"]),
                gross_selling=float(row["gross_selling"]),
                net_change=float(row["net_change"]),
            )
            for row in results
        ]

        return DailyFlowResponse(
            ticker=ticker, cusip=clean_cusip, daily_data=daily_data
        )

    except Exception as e:
        # Log error in production
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.get("/api/v1/search/companies_by_aum", response_model=list[dict])
def search_companies_by_aum(
    min_aum: Optional[int] = Query(None, description="Minimum AUM"),
    max_aum: Optional[int] = Query(None, description="Maximum AUM"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """
    Search for companies within a specific Assets Under Management (AUM) range.
    """
    # Start with a base query
    query = """
        SELECT
            cik_number,
            company_name,
            aum
        FROM companies
        WHERE 1=1
    """
    params = []

    # Dynamically build the WHERE clause based on provided parameters
    if min_aum is not None:
        query += " AND aum >= %s"
        params.append(min_aum)

    if max_aum is not None:
        query += " AND aum <= %s"
        params.append(max_aum)

    # Add sorting and pagination
    query += " ORDER BY aum DESC NULLS LAST LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    try:
        # Execute the query safely to prevent SQL injection
        db.execute(query, tuple(params))
        results = db.fetchall()

        # Format the response
        companies = [
            {
                "cik": str(row["cik_number"]),
                "company_name": row["company_name"],
                "aum": row["aum"],
            }
            for row in results
        ]
        return companies

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
