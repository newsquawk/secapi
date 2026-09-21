from pydantic import BaseModel
import datetime as dt
from typing import Optional, Dict, Any, List


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


class HoldingActivity(BaseModel):
    """Represents a single change (one 'headline') from a filing."""

    # Company Info
    cik: str
    company_name: str
    aum: Optional[int] = None

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

    # Change Info
    change_type: str  # 'new', 'closed', 'increased', 'decreased'
    current_shares: Optional[int] = None
    previous_shares: Optional[int] = None
    change_in_share: Optional[int] = None
    percent_change: Optional[float] = None

    # Value Info
    current_value: Optional[int] = None
    previous_value: Optional[int] = None
    absolute_value_change: int

    current_price_per_share: Optional[float] = None
    previous_price_per_share: Optional[float] = None

    # Downstream Facet Info (content-hub)
    weight_pct: Optional[float] = None
    value_pct: Optional[float] = None
    form_type: Optional[str] = None


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


class TopStockChangeEntry(BaseModel):
    issuer_name: str
    cusip: str
    ticker: Optional[str] = None
    net_shares_change: float
    net_value_change: float
    absolute_value_change: float
    gross_buying_shares: float
    gross_selling_shares: float


class TopStockChangesResponse(BaseModel):
    date: dt.date
    sort_by: str
    stocks: List[TopStockChangeEntry]


class FilingEnvelope(BaseModel):
    """Filing envelope carrying metadata and holding activities for downstream ingestion."""

    filing_id: int
    accession_number: str
    cik: str
    company_name: str
    form_type: str
    filing_date: dt.date
    period_of_report: dt.date
    aum: Optional[int] = None
    previous_filing_id: Optional[int] = None
    previous_accession_number: Optional[str] = None
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


class FilingsByAumResponse(BaseModel):
    filings: List[FilingListItem]
    pagination: FilingsPagination


class DataTablesHoldingsResponse(BaseModel):
    draw: int
    recordsTotal: int
    recordsFiltered: int
    data: List[Dict[str, Any]]

