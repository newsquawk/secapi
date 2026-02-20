from pydantic import BaseModel
import datetime as dt
from typing import Optional
from typing import List


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
    is_common_stock: bool

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

    # --- ADD THESE TWO LINES ---
    current_price_per_share: Optional[float] = None
    previous_price_per_share: Optional[float] = None


class LatestActivityResponse(BaseModel):
    """The response for the latest activity/headline list endpoint."""

    activities: List[HoldingActivity]
    has_next_page: bool = False


class FlowPoint(BaseModel):
    date: str
    gross_buying: float
    gross_selling: float
    net_change: float


class FlowResponse(BaseModel):
    cusip: str
    history: List[FlowPoint]


class DailyFlowEntry(BaseModel):
    date: dt.date
    gross_buying: float
    gross_selling: float
    net_change: float


class DailyFlowResponse(BaseModel):
    cusip: str
    daily_data: List[DailyFlowEntry]
