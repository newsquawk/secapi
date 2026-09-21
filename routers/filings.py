from typing import List, Optional
import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from config import logger
from database import get_db_cursor, INTERNAL_ERROR_DETAIL
from sec_models import (
    Filing,
    ManagerSummary,
    ManagerFilingsResponse,
    FilingsListResponse,
    FilingDetail,
    DataTablesHoldingsResponse,
    CompanySearchResult,
    CompanyAumRank,
    FilingsByAumResponse,
)

router = APIRouter()


def _format_address(
    street1: str,
    street2: str,
    city: str,
    state: str,
    state_desc: str,
    zipcode: str,
) -> str:
    """Format address components into a structured address."""
    street1 = street1 or ""
    street2 = street2 or ""
    city = city or ""
    state = state or ""
    state_desc = state_desc or ""
    zipcode = zipcode or ""

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

    return ", ".join(address_parts)


def _format_holdings_text(d: dict) -> str:
    """Format dictionary entries deterministically with sorted keys."""
    items = []
    for k in sorted(d.keys()):
        val = d[k]
        if isinstance(val, dict):
            items.append(f"{k}: {{{_format_holdings_text(val)}}}")
        elif isinstance(val, list):
            items.append(f"{k}: [{', '.join(str(x) for x in val)}]")
        else:
            items.append(f"{k}: {val}")
    return ", ".join(items)


# ---------------------------------------------------------------------------
# Managers Endpoints
# ---------------------------------------------------------------------------
@router.get("/managers", response_model=List[ManagerSummary], tags=["Managers"])
@router.get("/managers/", response_model=List[ManagerSummary], include_in_schema=False)
def get_managers(
    request: Request,
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Retrieve all managers with pagination."""
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
    logger.info(f"Executing query to fetch managers with limit={limit} and offset={offset}")
    db.execute(query, (limit, offset))
    results = db.fetchall()

    companies = []
    for row in results:
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

    logger.info(f"Fetched {len(companies)} managers from the database.")
    if response:
        response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
    return companies


@router.get("/managers/{cik}", response_model=ManagerSummary, tags=["Managers"])
def get_manager(
    request: Request,
    cik: str,
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """Retrieve details for a specific manager by CIK number."""
    try:
        clean_cik = cik.strip().lstrip("0") or "0"
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
            WHERE cik_number = %s OR cik_number = %s
            LIMIT 1
        """
        logger.info(f"Executing query to fetch manager with CIK={cik} (clean={clean_cik})")
        db.execute(query, (cik, clean_cik))
        result = db.fetchone()

        if not result:
            logger.error(f"Manager with CIK {cik} not found.")
            raise HTTPException(status_code=404, detail=f"Manager with CIK {cik} not found")

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

        company = {
            "cik": str(result.get("cik_number")),
            "company_name": result.get("company_name"),
            "company_phone": result.get("company_phone"),
            "mailing_address": mailing_address,
            "business_address": business_address,
        }

        if response:
            response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
        return company

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching manager with CIK {cik}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/managers/{cik}/filings", response_model=ManagerFilingsResponse, tags=["Managers"])
def get_manager_filings(
    request: Request,
    cik: str,
    limit: int = Query(100, ge=1, le=1000, description="Number of results to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """Retrieve all filings for a specific manager by CIK number."""
    try:
        logger.info(f"Fetching filings for manager with CIK={cik}, limit={limit}, offset={offset}")
        clean_cik = cik.strip().lstrip("0") or "0"
        db.execute(
            "SELECT company_id FROM companies WHERE cik_number = %s OR cik_number = %s LIMIT 1",
            (cik, clean_cik),
        )
        company: dict = db.fetchone()  # type: ignore
        if not company:
            logger.error(f"Manager with CIK {cik} not found when fetching filings.")
            raise HTTPException(status_code=404, detail=f"Manager with CIK {cik} not found")
        company_id = company["company_id"]  # type: ignore

        count_query = "SELECT COUNT(*) FROM filings WHERE company_id = %s"
        db.execute(count_query, (company_id,))
        total_count = db.fetchone()["count"]  # type: ignore

        if total_count == 0:
            logger.info(f"No filings found for manager with CIK={cik}")
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
        logger.info(
            f"Executing filings query for company_id={company_id} with limit={limit} and offset={offset}"
        )
        db.execute(filings_query, (company_id, limit, offset))
        filings_data = db.fetchall()

        filings = [Filing(**row) for row in filings_data]  # type: ignore
        has_more = (offset + len(filings)) < total_count

        if response:
            response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=120"

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
        logger.error(f"Error fetching filings for manager with CIK {cik}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


# ---------------------------------------------------------------------------
# Filings Endpoints
# ---------------------------------------------------------------------------
@router.get("/filings", response_model=FilingsListResponse, tags=["Filings"])
@router.get("/filings/", response_model=FilingsListResponse, include_in_schema=False)
def get_filings(
    request: Request,
    limit: int = Query(100, description="Number of items to return", ge=1, le=100),
    offset: int = Query(0, description="Number of items to skip", ge=0),
    sort_by: str = Query("filing_date", description="Column to sort by"),
    sort_order: str = Query("desc", description="Sort order: 'asc' or 'desc'"),
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """Retrieve filings with sorting and pagination metadata."""
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
        logger.error(f"Invalid sort column specified: {sort_by}")
        raise HTTPException(status_code=400, detail="Invalid sort column specified.")

    if sort_order.lower() not in ["asc", "desc"]:
        logger.error(f"Invalid sort order specified: {sort_order}")
        raise HTTPException(status_code=400, detail="Invalid sort order. Use 'asc' or 'desc'.")

    sort_column = allowed_sort_columns[sort_by]
    sort_order_str = sort_order.upper()

    try:
        if sort_by == "aum":
            count_query = """
                SELECT COUNT(*) as count 
                FROM companies c 
                WHERE EXISTS (SELECT 1 FROM filings f WHERE f.company_id = c.company_id)
            """
            db.execute(count_query)
            total_count = db.fetchone()["count"]

            filings_query = f"""
                SELECT
                    lf.accession_number, lf.form_type, lf.filing_date, lf.period_of_report,
                    lf.file_number, lf.filing_directory, lf.created_at, lf.updated_at,
                    c.company_name, c.cik_number, c.aum
                FROM (
                    SELECT company_id, company_name, cik_number, aum
                    FROM companies c
                    WHERE EXISTS (SELECT 1 FROM filings f WHERE f.company_id = c.company_id)
                    ORDER BY NULLIF(aum, 0) {sort_order_str} NULLS LAST, company_name ASC
                    LIMIT %s OFFSET %s
                ) c
                CROSS JOIN LATERAL (
                    SELECT f.accession_number, f.form_type, f.filing_date, f.period_of_report,
                           f.file_number, f.filing_directory, f.created_at, f.updated_at
                    FROM filings f
                    WHERE f.company_id = c.company_id
                    ORDER BY f.filing_date DESC, f.accession_number DESC
                    LIMIT 1
                ) lf
            """
            logger.info(f"Executing filings query with AUM sorting, limit={limit}, offset={offset}")
            db.execute(filings_query, (limit, offset))
            filings_data = db.fetchall()
        else:
            count_query = "SELECT COALESCE(NULLIF(reltuples::bigint, 0), (SELECT count(*) FROM filings)) AS count FROM pg_class WHERE relname = 'filings'"
            db.execute(count_query)
            total_count = db.fetchone()["count"]  # type: ignore

            order_by_clause = f"{sort_column} {sort_order_str}"
            if sort_by == "filing_date":
                order_by_clause += f", f.created_at {sort_order_str}"
            else:
                order_by_clause += ", f.filing_date DESC, f.created_at DESC"

            if total_count == 0:
                logger.info("No filings found in the database.")
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
                ORDER BY {order_by_clause}
                LIMIT %s OFFSET %s
            """
            logger.info(f"Executing filings query with limit={limit} and offset={offset}")
            db.execute(filings_query, (limit, offset))
            filings_data = db.fetchall()

        has_more = (offset + len(filings_data)) < total_count

        if response:
            response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=120"

        return {
            "filings": filings_data,
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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching filings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/filings/{accession_number}", response_model=FilingDetail, tags=["Filings"])
def get_filing_by_accession_number(
    request: Request,
    accession_number: str,
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """Retrieve filing metadata for a specific accession number."""
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
        logger.info("Executing query to fetch filing with accession number: %s", accession_number)
        db.execute(query, (accession_number,))
        result = db.fetchone()

        if not result:
            logger.error(f"Filing with accession number {accession_number} not found.")
            raise HTTPException(
                status_code=404,
                detail=f"Filing with accession number {accession_number} not found",
            )

        if response:
            response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800, immutable"
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching filing with accession number {accession_number}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


# ---------------------------------------------------------------------------
# Holdings Endpoints (DataTables)
# ---------------------------------------------------------------------------
@router.get("/holdings/{accession_number}", response_model=DataTablesHoldingsResponse, tags=["Holdings"])
def get_holding_by_accession_number(
    request: Request,
    accession_number: str,
    draw: int = Query(0, ge=0, alias="draw"),
    start: int = Query(0, ge=0, alias="start"),
    length: int = Query(10, ge=-1, le=1000, alias="length"),
    search_value: Optional[str] = Query(None, alias="search[value]"),
    order_column_index: int = Query(0, alias="order[0][column]"),
    order_dir: str = Query("asc", alias="order[0][dir]"),
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """Retrieve holdings for a single filing, supporting server-side DataTables pagination and search."""
    try:
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

        effective_length = 5000 if length == -1 else length

        db.execute(
            "SELECT filing_id FROM filings WHERE accession_number = %s LIMIT 1",
            (accession_number,),
        )
        filing_row = db.fetchone()
        if not filing_row:
            raise HTTPException(
                status_code=404,
                detail=f"Holdings for accession number {accession_number} not found",
            )
        filing_id = filing_row["filing_id"]

        db.execute("SELECT count(*) FROM holdings WHERE filing_id = %s", (filing_id,))
        total_records = db.fetchone()["count"]

        where_clause = "WHERE h.filing_id = %s"
        search_params = [filing_id]

        if search_value and search_value.strip():
            where_clause += """ AND (
                i.issuer_name ILIKE %s OR
                i.cusip ILIKE %s OR
                t.name ILIKE %s OR
                s.name ILIKE %s OR
                d.name ILIKE %s OR
                p.name ILIKE %s
            )"""
            search_param = f"%{search_value.strip()}%"
            search_params.extend([search_param] * 6)

        count_query = f"""
            SELECT count(*)
            FROM holdings_normalised h
            LEFT JOIN issuers i ON h.issuer_id = i.issuer_id
            LEFT JOIN title_of_class_table t ON h.title_of_class = t.id
            LEFT JOIN ssh_prnamt_type_table s ON h.shares_or_principal_type = s.id
            LEFT JOIN put_or_call_table p ON h.put_or_call = p.id
            LEFT JOIN investment_discretion_table d ON h.investment_discretion = d.id
            {where_clause}
        """
        db.execute(count_query, search_params)
        filtered_records = db.fetchone()["count"]

        holdings_query = f"""
            SELECT
                i.issuer_name,
                i.cusip,
                t.name AS title_of_class,
                h.value,
                h.shares_or_principal_amount,
                s.name AS shares_or_principal_type,
                p.name AS put_or_call,
                d.name AS investment_discretion,
                h.voting_authority_sole,
                h.voting_authority_shared,
                h.voting_authority_none
            FROM holdings_normalised h
            LEFT JOIN issuers i ON h.issuer_id = i.issuer_id
            LEFT JOIN title_of_class_table t ON h.title_of_class = t.id
            LEFT JOIN ssh_prnamt_type_table s ON h.shares_or_principal_type = s.id
            LEFT JOIN put_or_call_table p ON h.put_or_call = p.id
            LEFT JOIN investment_discretion_table d ON h.investment_discretion = d.id
            {where_clause}
            ORDER BY {order_by_column} {order_direction}
            OFFSET %s LIMIT %s;
        """
        query_params = search_params + [start, effective_length]
        logger.info(
            f"Executing holdings query for accession_number={accession_number} with search='{search_value}', start={start}, length={length}"
        )
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

        if response:
            response.headers["Cache-Control"] = (
                "public, max-age=86400, stale-while-revalidate=604800, immutable"
            )
        return {
            "draw": draw,
            "recordsTotal": total_records,
            "recordsFiltered": filtered_records,
            "data": formatted_data,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching holdings for accession number {accession_number}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


# ---------------------------------------------------------------------------
# Search Endpoints
# ---------------------------------------------------------------------------
@router.get("/api/search/companies", response_model=List[CompanySearchResult], tags=["Search"])
def search_companies(
    request: Request,
    q: str = Query(..., min_length=2, description="Search term for company name or CIK"),
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """Search for companies by name or CIK for autocomplete."""
    clean_q = q.strip()
    if len(clean_q) < 2:
        return []

    try:
        if clean_q.isdigit():
            search_cik = f"{clean_q}%"
            search_name = f"%{clean_q}%"
            query = """
                (
                    SELECT company_name, cik_number
                    FROM companies
                    WHERE cik_number::text LIKE %s
                    ORDER BY cik_number ASC
                    LIMIT 10
                )
                UNION
                (
                    SELECT company_name, cik_number
                    FROM companies
                    WHERE company_name ILIKE %s
                    LIMIT 10
                )
                LIMIT 10;
            """
            db.execute(query, (search_cik, search_name))
        else:
            search_name = f"%{clean_q}%"
            query = """
                SELECT company_name, cik_number
                FROM companies
                WHERE company_name ILIKE %s
                LIMIT 10;
            """
            db.execute(query, (search_name,))

        results = db.fetchall()

        companies = [
            {"name": row["company_name"], "cik": str(row["cik_number"])}
            for row in results
        ]
        if response:
            response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
        return companies

    except Exception as e:
        logger.error(f"Error during company search: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/api/v1/search/companies_by_aum", response_model=List[CompanyAumRank], tags=["Search"])
def search_companies_by_aum(
    request: Request,
    min_aum: Optional[int] = Query(None, description="Minimum AUM"),
    max_aum: Optional[int] = Query(None, description="Maximum AUM"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """Search for companies within a specific Assets Under Management (AUM) range."""
    query = """
        SELECT
            cik_number,
            company_name,
            aum
        FROM companies
        WHERE 1=1
    """
    params = []

    if min_aum is not None:
        query += " AND aum >= %s"
        params.append(min_aum)

    if max_aum is not None:
        query += " AND aum <= %s"
        params.append(max_aum)

    query += " ORDER BY aum DESC NULLS LAST LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    try:
        db.execute(query, tuple(params))
        results = db.fetchall()

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
        logger.error(f"Error searching companies by AUM: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/api/v1/search/filings_by_aum", response_model=FilingsByAumResponse, tags=["Search"])
def search_filings_by_aum(
    request: Request,
    min_aum: Optional[int] = Query(None, description="Minimum AUM filter"),
    max_aum: Optional[int] = Query(None, description="Maximum AUM filter"),
    ciks: Optional[List[str]] = Query(None, description="List of company CIKs to include"),
    limit: int = Query(100, ge=1, le=1000, description="Number of results to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    sort_by: str = Query(
        "created_at",
        description="Sort column: 'filing_date' or 'created_at' or 'period_of_report'",
    ),
    sort_order: str = Query("desc", description="Sort order: 'asc' or 'desc'"),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """Search and filter filings based on Company AUM and specific CIKs with pagination and sorting."""
    allowed_sort_columns = {
        "filing_date": "f.filing_date",
        "created_at": "f.created_at",
        "period_of_report": "f.period_of_report",
    }

    if sort_by not in allowed_sort_columns:
        logger.error(f"Invalid sort column specified: {sort_by}")
        raise HTTPException(status_code=400, detail="Invalid sort column specified.")

    if sort_order.lower() not in ["asc", "desc"]:
        logger.error(f"Invalid sort order specified: {sort_order}")
        raise HTTPException(status_code=400, detail="Invalid sort order. Use 'asc' or 'desc'.")

    sort_column = allowed_sort_columns[sort_by]
    sort_dir = sort_order.upper()

    try:
        where_clauses = ["1=1"]
        params = {}

        if min_aum is not None:
            where_clauses.append("c.aum >= %(min_aum)s")
            params["min_aum"] = min_aum

        if max_aum is not None:
            where_clauses.append("c.aum <= %(max_aum)s")
            params["max_aum"] = max_aum

        if ciks:
            clean_ciks = [c.strip().lstrip("0") or "0" for c in ciks if c.strip()]
            all_ciks = list(set(ciks + clean_ciks))
            where_clauses.append("c.cik_number = ANY(%(ciks)s)")
            params["ciks"] = all_ciks

        where_sql = " AND ".join(where_clauses)

        count_query = f"""
            SELECT count(*)
            FROM filings f
            JOIN companies c ON f.company_id = c.company_id
            WHERE {where_sql}
        """
        db.execute(count_query, params)
        total_count = db.fetchone()["count"]

        if total_count == 0:
            return {
                "filings": [],
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "total": 0,
                    "has_more": False,
                },
            }

        filings_query = f"""
            SELECT
                f.accession_number, f.form_type, f.filing_date, f.period_of_report,
                f.file_number, f.filing_directory, f.created_at, f.updated_at,
                c.company_name, c.cik_number, c.aum
            FROM filings f
            JOIN companies c ON f.company_id = c.company_id
            WHERE {where_sql}
            ORDER BY {sort_column} {sort_dir}
            LIMIT %(limit)s OFFSET %(offset)s
        """
        params["limit"] = limit
        params["offset"] = offset

        db.execute(filings_query, params)
        filings_data = db.fetchall()

        has_more = (offset + len(filings_data)) < total_count

        return {
            "filings": filings_data,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total_count,
                "has_more": has_more,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error searching filings by AUM: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)
