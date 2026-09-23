import asyncio
import hashlib
from typing import Optional
import polars as pl
import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import (
    OPENROUTER_API_KEY,
    AI_MODEL,
    DEEPSEEK_API_KEY,
    RATE_LIMIT,
    OPTION_PUT_CALL_NAMES,
    CUSIP_TO_TICKER,
    COMPARE_CACHE_MAX_SIZE,
    COMPARE_CACHE,
    AI_SUMMARY_CACHE_MAX_SIZE,
    AI_SUMMARY_CACHE,
    CUSIP_DETAILS_DF,
    limiter,
    logger,
    client,
)
from database import get_db_cursor, INTERNAL_ERROR_DETAIL
from sec_models import HoldingsRequest
from routers.filings import _format_holdings_text

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas & Queries
# ---------------------------------------------------------------------------
class ComparisonRequest(BaseModel):
    acc_num_1: str
    acc_num_2: str


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
        i.issuer_name,
        COALESCE(t.is_common_stock, FALSE) AS is_common_stock,
        h.shares_or_principal_amount,
        h.value,
        p.name AS put_or_call,
        i.cusip,
        i.sic
    FROM holdings_normalised h
    LEFT JOIN issuers i ON h.issuer_id = i.issuer_id
    LEFT JOIN title_of_class_table t ON h.title_of_class = t.id
    LEFT JOIN put_or_call_table p ON h.put_or_call = p.id
    WHERE h.filing_id = (SELECT filing_id FROM filings WHERE accession_number = %s)
    ORDER BY h.value DESC
    LIMIT 25000
"""

HOLDINGS_SCHEMA = {
    "issuer_name": pl.Utf8,
    "is_common_stock": pl.Boolean,
    "shares_or_principal_amount": pl.Int64,
    "value": pl.Int64,
    "put_or_call": pl.Utf8,
    "cusip": pl.Utf8,
    "sic": pl.Int64,
}


def _df_to_dict_list(df: pl.DataFrame):
    """Convert DataFrame to list of dictionaries."""
    if df.is_empty():
        return []
    return df.to_dicts()


def _process_filings(df: pl.DataFrame, latest: bool = True):
    suffix = "latest" if latest else "prev"

    if df.is_empty():
        logger.warning(
            f"No holdings data found for {'latest' if latest else 'previous'} filing."
        )
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
    if median_price is not None and median_price < 2.0:
        requires_multiplication = True

    _is_option = pl.col("put_or_call").is_not_null() & pl.col(
        "put_or_call"
    ).str.to_uppercase().is_in(OPTION_PUT_CALL_NAMES)

    updated_df = (
        df_with_prices.filter(pl.col("is_common_stock") & ~_is_option)
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
        logger.error(f"Error saving comparison: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/api/ai_summary")
@limiter.limit(RATE_LIMIT)
async def openai_call(
    request: Request,
    payload: HoldingsRequest = Body(...),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    api_key_configured = OPENROUTER_API_KEY or DEEPSEEK_API_KEY
    if not api_key_configured or not client:
        logger.error("Neither OPENROUTER_API_KEY nor DEEPSEEK_API_KEY is configured. Cannot call AI API.")
        return JSONResponse(
            content={"summary": "API key not configured"}, status_code=503
        )

    new_holdings = pl.DataFrame(payload.new_holdings)
    closed_positions = pl.DataFrame(payload.closed_positions)
    increased_holdings = pl.DataFrame(payload.increased_holdings)
    decreased_holdings = pl.DataFrame(payload.decreased_holdings)

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
            ["change_in_share", "percent_change"],
            descending=False,
        ).head(5)

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

    new_holding_text = _format_holdings_text(new_holdings_dict)
    closed_positions_text = _format_holdings_text(closed_positions_dict)
    increased_holdings_text = _format_holdings_text(increased_holdings_dict)
    decreased_holdings_text = _format_holdings_text(decreased_holdings_dict)

    combined_input = f"{new_holding_text}#{closed_positions_text}#{increased_holdings_text}#{decreased_holdings_text}"
    if not combined_input.replace("#", "").strip():
        return JSONResponse(
            content={"summary": "No significant position changes detected for this filing."},
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    cache_key = hashlib.sha256(combined_input.encode("utf-8")).hexdigest()

    # Tier 1: LRU cache
    if cache_key in AI_SUMMARY_CACHE:
        logger.info(f"Serving AI summary for {cache_key[:8]} from in-memory cache")
        AI_SUMMARY_CACHE.move_to_end(cache_key)
        return JSONResponse(
            content={"summary": AI_SUMMARY_CACHE[cache_key]},
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    # Tier 2: DB cache
    if db:
        try:
            db.execute(
                "SELECT summary FROM ai_summaries WHERE cache_key = %s;",
                (cache_key,),
            )
            cached_row = db.fetchone()
            if cached_row and cached_row.get("summary"):
                summary_text = cached_row["summary"]
                logger.info(f"Serving AI summary for {cache_key[:8]} from database cache")
                AI_SUMMARY_CACHE[cache_key] = summary_text
                if len(AI_SUMMARY_CACHE) > AI_SUMMARY_CACHE_MAX_SIZE:
                    AI_SUMMARY_CACHE.popitem(last=False)
                return JSONResponse(
                    content={"summary": summary_text},
                    headers={"Cache-Control": "public, max-age=86400, immutable"},
                )
        except Exception as e:
            logger.warning(f"Error checking ai_summaries table: {e}")

    # Tier 3: Call DeepSeek
    async def _get_summary(title, data_text):
        if not data_text.strip():
            return ""
        prompt = f"""
        Generate a summary of the holdings changes for the fund management in one or two sentences.
        {title}: {data_text}
        """
        response = await client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are a professional financial analyst. Be concise.",
                },
                {"role": "user", "content": prompt},
            ],
            stream=False,
            timeout=15.0,
        )
        return response.choices[0].message.content

    api_calls = [
        _get_summary("New Holdings", new_holding_text),
        _get_summary("Closed Positions", closed_positions_text),
        _get_summary("Increased Holdings", increased_holdings_text),
        _get_summary("Decreased Holdings", decreased_holdings_text),
    ]
    try:
        texts = await asyncio.gather(*api_calls)
        final_summary = " ".join([text for text in texts if text]).strip()
        if not final_summary:
            final_summary = "No significant position changes detected."

        if db:
            try:
                db.execute(
                    """
                    INSERT INTO ai_summaries (cache_key, summary)
                    VALUES (%s, %s)
                    ON CONFLICT (cache_key) DO NOTHING;
                    """,
                    (cache_key, final_summary),
                )
                if db.connection and not db.connection.closed:
                    db.connection.commit()
                logger.info(f"Persisted AI summary for {cache_key[:8]} to database")
            except Exception as e:
                logger.warning(f"Error persisting AI summary to database: {e}")

        AI_SUMMARY_CACHE[cache_key] = final_summary
        if len(AI_SUMMARY_CACHE) > AI_SUMMARY_CACHE_MAX_SIZE:
            AI_SUMMARY_CACHE.popitem(last=False)

        return JSONResponse(
            content={"summary": final_summary},
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )
    except Exception as e:
        logger.error(f"Error calling OpenAI API: {e}", exc_info=True)
        return JSONResponse(
            content={"summary": "AI summary is currently unavailable due to a provider error."},
            status_code=502,
        )


@router.get("/analysis/{previous_accession}/{latest_accession}", response_model=dict)
def compare_holdings(
    request: Request,
    previous_accession: str,
    latest_accession: str,
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """Compare two holdings by their accession numbers."""
    try:
        acc_latest = latest_accession.strip()
        acc_prev = previous_accession.strip()

        cache_key = f"{acc_prev}_{acc_latest}"
        if cache_key in COMPARE_CACHE:
            logger.info(f"Serving comparison for {cache_key} from in-memory cache")
            COMPARE_CACHE.move_to_end(cache_key)
            if response:
                response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800, immutable"
                response.headers["ETag"] = f'"{hashlib.sha256(cache_key.encode()).hexdigest()[:16]}"'
            return COMPARE_CACHE[cache_key]

        db.execute(FILING_QUERY_WITH_PRIORITY, (acc_prev, acc_prev))
        previous_filing = db.fetchone()

        db.execute(FILING_QUERY_WITH_PRIORITY, (acc_latest, acc_latest))
        latest_filing = db.fetchone()

        if not previous_filing:
            logger.error("Previous filing with accession number %s not found.", acc_prev)
            raise HTTPException(
                status_code=404, detail=f"Previous filing {acc_prev} not found"
            )
        if not latest_filing:
            logger.error("Latest filing with accession number %s not found.", acc_latest)
            raise HTTPException(
                status_code=404, detail=f"Latest filing {acc_latest} not found"
            )

        latest_acc = latest_filing["accession_number"]
        previous_acc = previous_filing["accession_number"]

        if previous_filing["cik_number"] != latest_filing["cik_number"]:
            logger.error(
                "CIK mismatch: Previous filing CIK %s does not match Latest filing CIK %s",
                previous_filing["cik_number"],
                latest_filing["cik_number"],
            )
            return {"error": "CIK for latest and previous quarters do not match"}

        if previous_filing["period_of_report"] > latest_filing["period_of_report"]:
            previous_filing, latest_filing = latest_filing, previous_filing
            acc_prev, acc_latest = previous_acc, latest_acc

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

        api_save_comparison(previous_acc, latest_acc, db)

        db.execute(HOLDINGS_QUERY, (acc_prev,))
        previous_holdings_data = db.fetchall()

        db.execute(HOLDINGS_QUERY, (acc_latest,))
        latest_holdings_data = db.fetchall()

        if not previous_holdings_data:
            previous_df = pl.DataFrame([], schema=HOLDINGS_SCHEMA)
        else:
            previous_df = pl.DataFrame(previous_holdings_data, schema=HOLDINGS_SCHEMA)

        if not latest_holdings_data:
            latest_df = pl.DataFrame([], schema=HOLDINGS_SCHEMA)
        else:
            latest_df = pl.DataFrame(latest_holdings_data, schema=HOLDINGS_SCHEMA)

        latest_aggregated, latest_multiplication = _process_filings(latest_df)
        prev_aggregated, prev_multiplication = _process_filings(previous_df, latest=False)

        _is_option = pl.col("put_or_call").is_not_null() & pl.col(
            "put_or_call"
        ).str.to_uppercase().is_in(OPTION_PUT_CALL_NAMES)

        latest_other_securities = latest_df.filter(
            ~pl.col("is_common_stock") | _is_option
        )
        prev_other_securities = previous_df.filter(
            ~pl.col("is_common_stock") | _is_option
        )

        latest_other_aggregated = (
            latest_other_securities.with_columns(
                pl.col("issuer_name")
                .str.strip_chars()
                .str.to_uppercase()
                .alias("issuer_name_clean")
            )
            .group_by(["cusip", "put_or_call"])
            .agg(
                pl.col("issuer_name_clean").first().alias("issuer_name_clean"),
                pl.col("shares_or_principal_amount").sum().alias("total_units_latest"),
                pl.col("value").sum().alias("total_value_latest"),
            )
        )

        prev_other_aggregated = (
            prev_other_securities.with_columns(
                pl.col("issuer_name")
                .str.strip_chars()
                .str.to_uppercase()
                .alias("issuer_name_clean")
            )
            .group_by(["cusip", "put_or_call"])
            .agg(
                pl.col("issuer_name_clean").first().alias("issuer_name_clean"),
                pl.col("shares_or_principal_amount").sum().alias("total_units_prev"),
                pl.col("value").sum().alias("total_value_prev"),
            )
        )

        merged_df = latest_aggregated.join(
            prev_aggregated, on="cusip", how="full", suffix="_prev"
        )
        merged_other_df = latest_other_aggregated.join(
            prev_other_aggregated, on="cusip", how="full", suffix="_prev"
        )

        if not CUSIP_DETAILS_DF.is_empty():
            if "sic" in merged_df.columns:
                merged_df = merged_df.drop(["sic", "sic_prev"])
            merged_df = merged_df.join(CUSIP_DETAILS_DF, on="cusip", how="left")
            merged_other_df = merged_other_df.join(
                CUSIP_DETAILS_DF, on="cusip", how="left"
            )

        # Sector Analysis
        sector_changes = (
            merged_df.filter(pl.col("sicSector").is_not_null())
            .group_by("sicSector")
            .agg(
                pl.col("total_value_latest").fill_null(0).sum().alias("latest_sector_total"),
                pl.col("total_value_prev").fill_null(0).sum().alias("prev_sector_total"),
            )
            .with_columns(
                pl.when(pl.col("prev_sector_total") > 0)
                .then(
                    (
                        (pl.col("latest_sector_total") - pl.col("prev_sector_total"))
                        / pl.col("prev_sector_total")
                    )
                    * 100
                )
                .otherwise(None)
                .round(2)
                .alias("percent_change")
            )
        )

        increased_sectors = sector_changes.filter(
            (pl.col("percent_change") > 0) | (pl.col("prev_sector_total") == 0)
        ).sort("percent_change", descending=True)

        decreased_sectors = sector_changes.filter(pl.col("percent_change") < 0).sort(
            "percent_change", descending=False
        )

        industry_changes = (
            merged_df.filter(pl.col("industry").is_not_null())
            .group_by("industry")
            .agg(
                pl.col("total_value_latest").fill_null(0).sum().alias("latest_total"),
                pl.col("total_value_prev").fill_null(0).sum().alias("prev_total"),
            )
            .with_columns(
                pl.when(pl.col("prev_total") > 0)
                .then(
                    (
                        (pl.col("latest_total") - pl.col("prev_total"))
                        / pl.col("prev_total")
                    )
                    * 100
                )
                .otherwise(None)
                .round(2)
                .alias("percent_change")
            )
        )

        inc_industries = industry_changes.filter(
            (pl.col("percent_change") > 0) | (pl.col("prev_total") == 0)
        ).sort("percent_change", descending=True, nulls_last=False)

        dec_industries = industry_changes.filter(pl.col("percent_change") < 0).sort(
            "percent_change", descending=False
        )

        sic_changes = (
            merged_df.filter(pl.col("sic").is_not_null())
            .group_by("sic")
            .agg(
                pl.col("total_value_latest").fill_null(0).sum().alias("latest_total"),
                pl.col("total_value_prev").fill_null(0).sum().alias("prev_total"),
            )
            .with_columns(
                pl.when(pl.col("prev_total") > 0)
                .then(
                    (
                        (pl.col("latest_total") - pl.col("prev_total"))
                        / pl.col("prev_total")
                    )
                    * 100
                )
                .otherwise(None)
                .round(2)
                .alias("percent_change")
            )
        )

        inc_sics = sic_changes.filter(
            (pl.col("percent_change") > 0) | (pl.col("prev_total") == 0)
        ).sort("percent_change", descending=True, nulls_last=False)

        dec_sics = sic_changes.filter(pl.col("percent_change") < 0).sort(
            "percent_change", descending=False
        )

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
            "cusip",
        )
        closed_other_positions = merged_other_df.filter(
            pl.col("total_units_latest").is_null()
        ).select(
            "issuer_name_clean_prev",
            "total_units_prev",
            "total_value_prev",
            "put_or_call",
            "cusip_prev",
        )

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

        increased_holdings = common_holdings.filter(pl.col("change_in_share") > 0)
        decreased_holdings = common_holdings.filter(pl.col("change_in_share") < 0)
        unchanged_holdings = common_holdings.filter(pl.col("change_in_share") == 0)

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
                "cusip",
            )
        )

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

        def inject_tickers(data_list, cusip_key="cusip"):
            if not data_list:
                return []
            for item in data_list:
                val = item.get(cusip_key)
                item["ticker"] = CUSIP_TO_TICKER.get(val)
            return data_list

        is_truncated = bool(
            (previous_holdings_data and len(previous_holdings_data) >= 25000)
            or (latest_holdings_data and len(latest_holdings_data) >= 25000)
        )
        response_data = {
            "metadata": {
                "cik": latest_filing.get("cik_number"),
                "company_name": latest_filing.get("company_name"),
                "truncated": is_truncated,
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
                        "by_sector": {
                            "increased": increased_sectors.to_dicts(),
                            "decreased": decreased_sectors.to_dicts(),
                        },
                        "by_industry": {
                            "increased": inc_industries.to_dicts(),
                            "decreased": dec_industries.to_dicts(),
                        },
                        "by_sic": {
                            "increased": inc_sics.to_dicts(),
                            "decreased": dec_sics.to_dicts(),
                        },
                    },
                },
            },
            "holdings": {
                "top_holdings_by_value": inject_tickers(
                    top_5_holdings_by_value.to_dicts()
                ),
                "top_other_securities_by_value": inject_tickers(
                    top_5_holdings_other_by_value.to_dicts()
                ),
                "new_holdings": {
                    "top_5": inject_tickers(top_5_new_common.to_dicts()),
                    "common_stock": inject_tickers(_df_to_dict_list(new_holdings)),
                    "other_securities": inject_tickers(
                        _df_to_dict_list(new_other_holdings)
                    ),
                },
                "closed_positions": {
                    "top_5": inject_tickers(
                        top_5_closed_common.to_dicts(), "cusip_prev"
                    ),
                    "common_stock": inject_tickers(
                        _df_to_dict_list(closed_positions), "cusip_prev"
                    ),
                    "other_securities": inject_tickers(
                        _df_to_dict_list(closed_other_positions), "cusip_prev"
                    ),
                },
                "increased_holdings": {
                    "top_5": inject_tickers(top_5_increased_common.to_dicts()),
                    "common_stock": inject_tickers(
                        _df_to_dict_list(increased_holdings)
                    ),
                    "other_securities": inject_tickers(
                        _df_to_dict_list(increased_other_holdings)
                    ),
                },
                "decreased_holdings": {
                    "top_5": inject_tickers(top_5_decreased_common.to_dicts()),
                    "common_stock": inject_tickers(
                        _df_to_dict_list(decreased_holdings)
                    ),
                    "other_securities": inject_tickers(
                        _df_to_dict_list(decreased_other_holdings)
                    ),
                },
                "common_holdings": {
                    "common_stock": inject_tickers(
                        _df_to_dict_list(unchanged_holdings)
                    ),
                    "other_securities": inject_tickers(
                        _df_to_dict_list(unchanged_other_holdings)
                    ),
                },
            },
        }

        COMPARE_CACHE[cache_key] = response_data
        if len(COMPARE_CACHE) > COMPARE_CACHE_MAX_SIZE:
            COMPARE_CACHE.popitem(last=False)

        if response:
            response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800, immutable"
            response.headers["ETag"] = f'"{hashlib.sha256(cache_key.encode()).hexdigest()[:16]}"'

        return response_data

    except HTTPException as e:
        logger.error(f"HTTPException occurred: {e.detail}")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/comparisons", response_model=list)
def get_recent_comparisons(
    request: Request,
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
        logger.info(f"Fetching recent comparisons with limit {limit} and offset {offset}")
        db.execute(query, (limit, offset))
        comparisons = db.fetchall()
        return comparisons
    except Exception as e:
        logger.error(f"Error fetching recent comparisons: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.post("/comparisons")
def save_comparison(
    request: Request,
    comparison_request: ComparisonRequest,
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    try:
        query = """
            INSERT INTO recent_comparisons (accession_number_1, accession_number_2)
            VALUES (%s, %s)
        """
        db.execute(query, (comparison_request.acc_num_1, comparison_request.acc_num_2))
        db.connection.commit()
        return {"message": "Comparison saved successfully"}
    except Exception as e:
        logger.error(f"Error saving comparison: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


@router.get("/company/{cik}/compare/latest")
def compare_latest_filings(
    request: Request,
    cik: str,
    response: Response = Response(),
    db: psycopg2.extensions.cursor = Depends(get_db_cursor),
):
    """Automatically compares the latest two quarterly filings for a given company CIK."""
    try:
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

        logger.info(f"Comparing filings {previous_filing} and {latest_filing} for CIK {cik}")

        result = compare_holdings(
            request=request,
            previous_accession=previous_filing,
            latest_accession=latest_filing,
            response=response,
            db=db,
        )
        if response:
            response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error comparing latest filings for CIK {cik}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)
