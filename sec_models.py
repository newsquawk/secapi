from pydantic import BaseModel
import datetime as dt
from typing import Optional, Dict, Any, List, Union


def compute_statutory_deadline(
    period_of_report: Union[dt.date, str, dt.datetime, None]
) -> Optional[dt.date]:
    """
    Authoritative SEC Rule 13f-1(a)(1) & Rule 0-3(a) deadline calculation:
    1. Base deadline = period_of_report + 45 calendar days.
    2. Weekend roll-forward: Saturday -> Monday (+2 days), Sunday -> Monday (+1 day).
    3. Washington's Birthday (Presidents' Day): If rolled deadline falls on the third
       Monday in February (days 15-21 inclusive), roll forward +1 day to Tuesday.
    """
    if not period_of_report:
        return None
    if isinstance(period_of_report, str):
        cleaned = period_of_report.strip()
        try:
            period_of_report = dt.date.fromisoformat(cleaned)
        except ValueError:
            period_of_report = dt.datetime.fromisoformat(cleaned).date()
    elif isinstance(period_of_report, dt.datetime):
        period_of_report = period_of_report.date()

    deadline = period_of_report + dt.timedelta(days=45)
    # Weekend roll-forward
    if deadline.weekday() == 5:  # Saturday
        deadline += dt.timedelta(days=2)
    elif deadline.weekday() == 6:  # Sunday
        deadline += dt.timedelta(days=1)

    # Federal holiday roll-forward (Presidents' Day)
    if deadline.month == 2 and deadline.weekday() == 0 and 15 <= deadline.day <= 21:
        deadline += dt.timedelta(days=1)

    return deadline


def compute_is_late_filing(
    filing_date: Union[dt.date, str, dt.datetime, None],
    period_of_report: Union[dt.date, str, dt.datetime, None],
) -> bool:
    """True if filing_date > statutory 45-day deadline after period_of_report."""
    if not filing_date or not period_of_report:
        return False
    if isinstance(filing_date, str):
        cleaned_f = filing_date.strip()
        try:
            filing_date = dt.date.fromisoformat(cleaned_f)
        except ValueError:
            filing_date = dt.datetime.fromisoformat(cleaned_f).date()
    elif isinstance(filing_date, dt.datetime):
        filing_date = filing_date.date()
    deadline = compute_statutory_deadline(period_of_report)
    if not deadline:
        return False
    return bool(filing_date > deadline)


def compute_is_amendment(form_type: Optional[str]) -> bool:
    """True if filing form indicates an amendment (e.g. 13F-HR/A, 13F-HR/A/A, 13F-NT/A)."""
    if not form_type:
        return False
    ft = str(form_type).strip().upper()
    return bool("/A" in ft or ft.endswith("/A"))


def compute_amendment_type(
    form_type: Optional[str],
    raw_amendment_type: Optional[str] = None,
) -> Optional[str]:
    """
    Distinguishes full portfolio restatements ('restatement') from new-holdings
    additions ('addition') on 13F-HR/A filings. Returns None for non-amendment filings.
    """
    if not compute_is_amendment(form_type):
        return None
    if raw_amendment_type:
        clean = str(raw_amendment_type).strip().lower()
        if clean in ("addition", "restatement"):
            return clean
        if "add" in clean or "new" in clean:
            return "addition"
        if "restate" in clean:
            return "restatement"
    return "restatement"


def enrich_filing_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Enriches a filing dictionary or model attributes with R4 flags."""
    f_type = d.get("form_type")
    is_amend = compute_is_amendment(f_type)
    d["is_amendment"] = is_amend
    d["amendment_type"] = compute_amendment_type(f_type, d.get("amendment_type"))
    f_date = d.get("filing_date")
    p_date = d.get("period_of_report")
    d["is_late_filing"] = compute_is_late_filing(f_date, p_date)
    return d


class Filing(BaseModel):
    """
    Represents a single SEC filing.
    """

    accession_number: str
    form_type: str
    filing_date: dt.date
    period_of_report: dt.date
    file_number: Optional[str] = None
    filing_directory: Optional[str] = None
    created_at: dt.datetime
    updated_at: dt.datetime
    reported_value_total: Optional[int] = None
    is_amendment: bool = False
    amendment_type: Optional[str] = None
    is_late_filing: bool = False



class Holding(BaseModel):
    """Represents a single holding within a filing."""

    holding_id: int
    issuer_name: str
    title_of_class: str
    shares_or_principal_amount: int
    shares_or_principal_type: str
    value: int
    put_or_call: Optional[str] = None
    investment_discretion: Optional[str] = None
    voting_authority_sole: Optional[int] = None
    voting_authority_shared: Optional[int] = None
    voting_authority_none: Optional[int] = None
    cusip: Optional[str] = None
    security_category: Optional[str] = None


class HoldingActivity(BaseModel):
    """Represents a single change (one 'headline') from a filing."""

    # Company Info
    cik: str
    company_name: str
    aum: Optional[int] = None
    reported_value_total: Optional[int] = None

    # Filing Info
    latest_accession_number: str
    previous_accession_number: Optional[str] = None
    reporting_period: dt.date
    filing_date: dt.date

    # Stock/Holding Info
    issuer_name: str
    cusip: str
    ticker: Optional[str] = None
    is_common_stock: bool
    put_or_call: Optional[str] = None
    # 'SH' (share count) or 'PRN' (principal amount in dollars); the *_shares
    # fields below hold that unit, and price_per_share is null for PRN.
    shares_or_principal_type: Optional[str] = None
    security_type: Optional[str] = None
    security_category: Optional[str] = None

    # Change Info
    change_type: str  # 'new', 'closed', 'increased', 'decreased', 'unchanged'
    current_shares: Optional[int] = None
    previous_shares: Optional[int] = None
    change_in_share: Optional[int] = None
    percent_change: Optional[float] = None
    split_factor: Optional[float] = None
    raw_previous_shares: Optional[int] = None
    raw_change_in_share: Optional[int] = None
    raw_percent_change: Optional[float] = None

    # Value Info
    current_value: Optional[int] = None
    previous_value: Optional[int] = None
    absolute_value_change: int

    current_price_per_share: Optional[float] = None
    previous_price_per_share: Optional[float] = None

    # Entry Type Info (R1)
    position_entry_type: Optional[str] = None
    entry_type: Optional[str] = None

    # Streak Info (R2)
    streak_quarters: Optional[int] = None
    streak_direction: Optional[str] = None
    streak_type: Optional[str] = None

    # Downstream Facet Info (content-hub)
    weight_pct: Optional[float] = None
    value_pct: Optional[float] = None
    form_type: Optional[str] = None
    is_amendment: bool = False
    amendment_type: Optional[str] = None
    is_late_filing: bool = False



class HoldingsRequest(BaseModel):
    new_holdings: List[Dict[str, Any]]
    closed_positions: List[Dict[str, Any]]
    increased_holdings: List[Dict[str, Any]]
    decreased_holdings: List[Dict[str, Any]]


class PaginationMetadata(BaseModel):
    """Unified pagination schema used across all collection endpoints."""
    limit: int
    offset: int
    total: Optional[int] = None
    has_more: bool
    next_offset: Optional[int] = None


class LatestActivityResponse(BaseModel):
    """The response for the latest activity/headline list endpoint."""

    activities: List[HoldingActivity]
    has_next_page: bool = False
    pagination: Optional[PaginationMetadata] = None


class FlowPoint(BaseModel):
    date: str
    gross_buying: float
    gross_selling: float
    net_change: float
    # New calculated field:
    net_change_pct_of_float: Optional[float] = None


class FlowResponse(BaseModel):
    cusip: str
    history: List[FlowPoint]


class DailyFlowEntry(BaseModel):
    date: dt.date
    gross_buying: float
    gross_selling: float
    net_change: float
    net_change_pct_float: Optional[float] = None
    percent_change: Optional[float] = None
    funds_buying: int = 0
    funds_selling: int = 0
    funds_new: int = 0
    funds_increased: int = 0
    funds_decreased: int = 0
    funds_closed: int = 0
    funds_unchanged: int = 0


class DailyFlowResponse(BaseModel):
    ticker: Optional[str] = None
    cusip: str
    free_float_shares: Optional[float] = None
    daily_data: List[DailyFlowEntry]


class AggregateFlowResponse(BaseModel):
    ticker: Optional[str] = None
    cusip: str
    days_looked_back: int
    gross_buying: float
    gross_selling: float
    net_change: float
    net_change_pct_float: Optional[float] = None
    percent_change: Optional[float] = None
    free_float_shares: Optional[float] = None
    funds_buying: int = 0
    funds_selling: int = 0
    funds_new: int = 0
    funds_increased: int = 0
    funds_decreased: int = 0
    funds_closed: int = 0
    funds_unchanged: int = 0


class TopStockChangeEntry(BaseModel):
    issuer_name: str
    cusip: str
    ticker: Optional[str] = None
    net_shares_change: float
    net_value_change: float
    absolute_value_change: float
    gross_buying_shares: float
    gross_selling_shares: float
    funds_buying: int = 0
    funds_selling: int = 0
    funds_new: int = 0
    funds_increased: int = 0
    funds_decreased: int = 0
    funds_closed: int = 0
    funds_unchanged: int = 0


class TopStockChangesResponse(BaseModel):
    date: dt.date
    sort_by: str
    stocks: List[TopStockChangeEntry]


class QuarterlyFlowEntry(BaseModel):
    quarter: dt.date
    gross_buying: float
    gross_selling: float
    net_change: float
    percent_change: Optional[float] = None
    funds_buying: int = 0
    funds_selling: int = 0
    funds_new: int = 0
    funds_increased: int = 0
    funds_decreased: int = 0
    funds_closed: int = 0
    funds_unchanged: int = 0


class QuarterlyFlowResponse(BaseModel):
    ticker: Optional[str] = None
    cusip: str
    quarterly_data: List[QuarterlyFlowEntry]


class FilingEnvelope(BaseModel):
    """
    A complete 13F filing for the Content Hub change feed: all filing-level
    fields plus every holding embedded in ``activities`` (common stock AND
    options, each tagged with is_common_stock / put_or_call). Content Hub owns
    the mapping/faceting/explosion — secapi hands over the whole record.
    """

    filing_id: int
    accession_number: str
    cik: str
    company_name: str
    form_type: str
    filing_date: dt.date
    period_of_report: dt.date
    aum: Optional[int] = None
    reported_value_total: Optional[int] = None
    # Filing-level metadata (lets Content Hub reproduce the Filings list widget).
    file_number: Optional[str] = None
    filing_directory: Optional[str] = None
    created_at: Optional[dt.datetime] = None
    updated_at: Optional[dt.datetime] = None
    previous_filing_id: Optional[int] = None
    previous_accession_number: Optional[str] = None
    is_amendment: bool = False
    amendment_type: Optional[str] = None
    is_late_filing: bool = False
    activities: List[HoldingActivity] = []


class ChangesResponse(BaseModel):
    """Cursor-paginated change feed response for content-hub."""

    items: List[FilingEnvelope]
    next_cursor: Optional[str] = None
    has_more: bool = False


class ChangesHeadResponse(BaseModel):
    """Head cursor response so pollers can tail from now."""

    head_cursor: str


class ManagerSummary(BaseModel):
    cik: str
    company_name: Optional[str] = None
    company_phone: Optional[str] = None
    mailing_address: Optional[str] = None
    business_address: Optional[str] = None


# Backward-compatible aliases
ManagerFilingsPagination = PaginationMetadata
FilingsPagination = PaginationMetadata
FilingsByAumPagination = PaginationMetadata


class ManagersListResponse(BaseModel):
    """Enveloped response for managers collection."""
    managers: List[ManagerSummary]
    pagination: PaginationMetadata


class ManagerFilingsResponse(BaseModel):
    filings: List[Filing]
    pagination: PaginationMetadata


class CompanySearchResult(BaseModel):
    name: str
    cik: str


class CompanyAumRank(BaseModel):
    cik: str
    company_name: str
    aum: Optional[int] = None


class CompaniesByAumResponse(BaseModel):
    """Enveloped response for company search by AUM."""
    companies: List[CompanyAumRank]
    pagination: PaginationMetadata


class FilingListItem(BaseModel):
    accession_number: str
    form_type: str
    filing_date: dt.date
    period_of_report: dt.date
    file_number: Optional[str] = None
    filing_directory: Optional[str] = None
    created_at: Optional[dt.datetime] = None
    updated_at: Optional[dt.datetime] = None
    company_name: Optional[str] = None
    cik_number: Optional[str] = None
    aum: Optional[int] = None
    reported_value_total: Optional[int] = None
    is_amendment: bool = False
    amendment_type: Optional[str] = None
    is_late_filing: bool = False



class FilingsSorting(BaseModel):
    current_sort_by: str
    current_sort_order: str


class FilingsListResponse(BaseModel):
    filings: List[FilingListItem]
    pagination: PaginationMetadata
    sorting: FilingsSorting


class FilingDetail(BaseModel):
    accession_number: str
    form_type: str
    filing_date: dt.date
    period_of_report: dt.date
    file_number: Optional[str] = None
    filing_directory: Optional[str] = None
    created_at: Optional[dt.datetime] = None
    updated_at: Optional[dt.datetime] = None
    company_name: Optional[str] = None
    cik_number: Optional[str] = None
    aum: Optional[int] = None
    reported_value_total: Optional[int] = None
    is_amendment: bool = False
    amendment_type: Optional[str] = None
    is_late_filing: bool = False



class FilingsByAumResponse(BaseModel):
    filings: List[FilingListItem]
    pagination: FilingsPagination


class DataTablesHoldingsResponse(BaseModel):
    draw: int
    recordsTotal: int
    recordsFiltered: int
    data: List[Dict[str, Any]]

