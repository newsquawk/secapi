# SEC Filings Analysis API 📊

A high-performance backend API built with **FastAPI** to serve and analyze SEC Form 13F filing data. It connects to a PostgreSQL database, executes fast data manipulation with **Polars**, and provides structured JSON endpoints for web frontends, market research tools, and institutional flow analysis.

---

## 📑 Table of Contents
- [✨ Key Features & Feeds](#-key-features--feeds)
- [📡 API Endpoints Reference](#-api-endpoints-reference)
  - [1. Activity Feeds & Story Streams](#1-activity-feeds--story-streams)
  - [2. Institutional Flow & Market Leaderboards](#2-institutional-flow--market-leaderboards)
  - [3. Manager & Company Endpoints](#3-manager--company-endpoints)
  - [4. Filings & Detailed Holdings](#4-filings--detailed-holdings)
  - [5. Portfolio Comparison & Analysis](#5-portfolio-comparison--analysis)
  - [6. AI Summaries & Health Check](#6-ai-summaries--health-check)
- [🛠️ Technology Stack](#️-technology-stack)
- [🚀 Getting Started](#-getting-started)
- [⚙️ Configuration](#️-configuration)
- [📚 Interactive API Documentation](#-interactive-api-documentation)

---

## ✨ Key Features & Feeds

- **Options Radar (`/activity/latest/options`)**: Tracks institutional Put & Call options trades (new positions, closed positions, increases, decreases) with filtering by ticker, contract type, action, and dollar value.
- **Stock Activity Feed (`/activity/latest/v3`)**: Flat stream of common stock portfolio changes across recently processed 13F filings.
- **Curated Stories (`/stories/latest/v2`)**: Generates structured summary cards showing top position changes for each filing.
- **Institutional Flow Analysis (`/api/v1/flow/...`)**: Aggregates net buying/selling pressure per stock with percentage of free float calculations.
- **Portfolio Diffing (`/analysis/...`)**: Instant side-by-side analysis between any two 13F filing periods.

---

## 📡 API Endpoints Reference

### 1. Activity Feeds & Story Streams

#### `GET /activity/latest/options` — Institutional Options Trades Feed
Retrieves institutional Put and Call trades from the latest batch of 13F filings. Excludes static/unchanged positions. Rows are aggregated per **filing × underlying security (CUSIP) × contract type**, so a fund holding both Puts and Calls on the same underlying ticker returns two rows.
* **Query Parameters**:
  * `ticker` *(string, optional)*: Filter by stock ticker or CUSIP (e.g. `NVDA`, `AAPL`).
  * `put_or_call` *(string, optional)*: Filter by contract type (`PUT` or `CALL`).
  * `change_type` *(string, optional)*: Filter by action (`new`, `closed`, `increased`, `decreased`).
  * `min_value` *(float, optional)*: Filter by minimum absolute dollar value change. Compared against values expressed in **thousands of dollars** (see Units below), e.g. `10000` ≈ $10M+.
  * `limit` *(int, default=10, 1-50)*: Number of fund filings to fetch (not the number of trade rows).
  * `offset` *(int, default=0)*: Number of fund filings to skip.
* **Response envelope**: `{ "activities": [ ... ], "has_next_page": boolean }`. `has_next_page` is `true` when more filings exist beyond the current `limit` / `offset` window.
* **`activities[]` object fields**:

  > 💡 **Consumer quick-reference** — the fields covering the "ticker / name / put-or-call / value / shares" contract are `ticker`, `issuer_name`, `put_or_call`, `current_value` / `previous_value` / `absolute_value_change`, and `current_shares` / `previous_shares` / `change_in_share`.

  | Field | Type | Description |
  | :--- | :--- | :--- |
  | `cik` | string | CIK of the filing investment manager |
  | `company_name` | string | Manager / fund name |
  | `aum` | integer \| null | Manager's assets under management |
  | `latest_accession_number` | string | Accession number of the latest filing |
  | `previous_accession_number` | string \| null | Accession number of the previous filing |
  | `reporting_period` | date (`YYYY-MM-DD`) | Period of report of the latest filing |
  | `filing_date` | date (`YYYY-MM-DD`) | Date the latest filing was submitted |
  | `issuer_name` | string | **📌 Name** — underlying issuer name (e.g. `TESLA MOTORS`) |
  | `cusip` | string | CUSIP of the underlying security |
  | `ticker` | string \| null | **📌 Ticker** — resolved from CUSIP; `null` if unmapped |
  | `is_common_stock` | boolean | Always `false` for the options feed |
  | `put_or_call` | string \| null | **📌 Contract type** — `Put` / `Call` (or `PUT` / `CALL`) |
  | `change_type` | string | `new`, `closed`, `increased`, or `decreased` |
  | `current_shares` | integer \| null | **📌 Shares** — contracts held in the latest filing |
  | `previous_shares` | integer \| null | Contracts held in the previous filing |
  | `change_in_share` | integer \| null | `current_shares - previous_shares` |
  | `percent_change` | float \| null | Percentage share change vs. previous filing (2 decimals) |
  | `current_value` | integer \| null | **📌 Value** — current position value in thousands of $ |
  | `previous_value` | integer \| null | Previous position value in thousands of $ |
  | `absolute_value_change` | integer | `abs(current_value - previous_value)` in thousands of $ |
  | `current_price_per_share` | float \| null | `current_value / current_shares` (2 decimals) |
  | `previous_price_per_share` | float \| null | `previous_value / previous_shares` (2 decimals) |

> ⚠️ **Units**: All `*_value` fields (and the `min_value` filter) use the SEC 13F convention of **thousands of dollars** — e.g. `current_value: 2500000` = **$2.5 billion**, not $2.5M.

* **Example**:
  ```bash
  curl "http://localhost:8000/activity/latest/options?ticker=TSLA&put_or_call=CALL&change_type=new"
  ```
* **Example response** (fields abridged for brevity):
  ```json
  {
    "activities": [
      {
        "cik": "0001318605",
        "company_name": "Tesla Inc",
        "aum": 123456789,
        "latest_accession_number": "0001067983-25-000123",
        "previous_accession_number": "0001067983-25-000098",
        "reporting_period": "2025-06-30",
        "filing_date": "2025-08-14",
        "issuer_name": "TESLA MOTORS",
        "cusip": "88160R101",
        "ticker": "TSLA",
        "is_common_stock": false,
        "put_or_call": "CALL",
        "change_type": "new",
        "current_shares": 10000,
        "previous_shares": null,
        "change_in_share": 10000,
        "percent_change": null,
        "current_value": 2500000,
        "previous_value": null,
        "absolute_value_change": 2500000,
        "current_price_per_share": 250.0,
        "previous_price_per_share": null
      }
    ],
    "has_next_page": false
  }
  ```

#### `GET /activity/latest/v3` — Common Stock Activity Stream
Retrieves a flat list of individual Common Stock position changes across the latest batch of filings.
* **Query Parameters**:
  * `limit` *(int, default=3, 1-50)*: Number of company filings to fetch.
  * `offset` *(int, default=0)*: Number of company filings to skip.
* **Example**:
  ```bash
  curl "http://localhost:8000/activity/latest/v3?limit=10"
  ```

#### `GET /stories/latest/v2` — Curated Story Highlights Feed
Aggregates recent filings into structured story cards highlighting top new, closed, increased, and decreased positions for both common stock and options.
* **Query Parameters**:
  * `limit` *(int, default=20, 1-50)*: Number of stories to return.
  * `offset` *(int, default=0)*: Number of stories to skip.
* **Example**:
  ```bash
  curl "http://localhost:8000/stories/latest/v2?limit=15"
  ```

---

### 2. Institutional Flow & Market Leaderboards

#### `GET /api/v1/flow/daily/{identifier}` — Daily Stock Flow Time Series
Aggregates institutional buying and selling volume grouped by **filing date** for a specific ticker or CUSIP, including net change as a percentage of free float.
* **Path Parameters**: `identifier` (e.g. `AAPL` or `037833100`).
* **Query Parameters**: `days` *(int, default=1, 1-365)*: Number of lookback days.
* **Example**:
  ```bash
  curl "http://localhost:8000/api/v1/flow/daily/NVDA?days=30"
  ```

#### `GET /api/v1/flow/aggregate/{identifier}` — Aggregate Stock Flow Summary
Returns a single consolidated summary of total buying, selling, and net flow over a given lookback window.
* **Path Parameters**: `identifier` (e.g. `MSFT` or `594918104`).
* **Query Parameters**: `days` *(int, default=14, 1-365)*: Number of lookback days.
* **Example**:
  ```bash
  curl "http://localhost:8000/api/v1/flow/aggregate/MSFT?days=14"
  ```

#### `GET /api/v1/flow/top-changes` — Top Market Changes Leaderboard
Retrieves the top 50 stocks with the largest absolute net movement across all 13F filings submitted on a target filing date.
* **Query Parameters**:
  * `date` *(YYYY-MM-DD, optional)*: Target filing date (defaults to today).
  * `sort_by` *(string, default="value")*: Sort by dollar value (`value`) or share volume (`shares`).
* **Example**:
  ```bash
  curl "http://localhost:8000/api/v1/flow/top-changes?sort_by=value&date=2026-02-14"
  ```

---

### 3. Manager & Company Endpoints

#### `GET /managers/` — List Investment Managers
Returns a paginated list of all investment managers.
* **Query Parameters**: `limit` *(default=100)*, `offset` *(default=0)*.
* **Example**: `curl "http://localhost:8000/managers/?limit=50"`

#### `GET /managers/{cik}` — Manager Metadata
Fetches detailed profile information for a specific manager by their CIK number.
* **Example**: `curl "http://localhost:8000/managers/0001067983"`

#### `GET /managers/{cik}/filings` — Manager Historical Filings
Lists all historical 13F filings for a specific manager CIK.
* **Example**: `curl "http://localhost:8000/managers/0001067983/filings"`

#### `GET /api/search/companies` — Autocomplete Search
Fast search for investment managers/companies by name prefix or CIK.
* **Query Parameters**: `q` *(string, required, min length 2)*.
* **Example**: `curl "http://localhost:8000/api/search/companies?q=Berkshire"`

#### `GET /api/v1/search/companies_by_aum` — Managers Ranked by AUM
Paginated list of investment managers ordered by total Assets Under Management.
* **Query Parameters**: `limit` *(default=20)*, `offset` *(default=0)*.

#### `GET /api/v1/search/filings_by_aum` — Filings Filtered by AUM Range
Searches filings from managers whose AUM falls within a specified minimum and maximum bracket.
* **Query Parameters**: `min_aum` *(optional)*, `max_aum` *(optional)*, `limit` *(default=20)*, `offset` *(default=0)*.

---

### 4. Filings & Detailed Holdings

#### `GET /filings/` — Master Filings Stream
Returns a paginated and sortable list of all processed 13F filings.
* **Query Parameters**:
  * `sort_by` *(default="filing_date")*: `filing_date`, `aum`, `company_name`, `cik_number`, `form_type`, `period_of_report`, `created_at`.
  * `sort_order` *(default="desc")*: `asc` or `desc`.
  * `limit` *(default=100, 1-100)*, `offset` *(default=0)*.
* **Example**:
  ```bash
  curl "http://localhost:8000/filings/?sort_by=aum&sort_order=desc&limit=25"
  ```

#### `GET /filings/{accession_number}` — Single Filing Details
Returns metadata for a specific filing by its SEC accession number.
* **Example**: `curl "http://localhost:8000/filings/0001067983-26-000012"`

#### `GET /holdings/{accession_number}` — Detailed Holdings (DataTables.js Compatible)
Provides paginated, searchable, and sortable holdings data within a specific filing.
* **Query Parameters**: `start` *(int)*, `length` *(int)*, `search[value]` *(string)*, `order[0][column]` *(int)*, `order[0][dir]` *(asc/desc)*.
* **Example**: `curl "http://localhost:8000/holdings/0001067983-26-000012?start=0&length=50"`

---

### 5. Portfolio Comparison & Analysis

#### `GET /analysis/{previous_accession}/{latest_accession}` — Two-Filing Portfolio Comparison
Compares two filings for a manager and returns a comprehensive breakdown of changes:
* **Common Stock**: `new_holdings`, `closed_positions`, `increased_holdings`, `decreased_holdings`, `unchanged_holdings`.
* **Other Securities / Options**: `new_other_securities`, `closed_other_securities`, `increased_other_securities`, `decreased_other_securities`, `unchanged_other_securities`.
* **Example**:
  ```bash
  curl "http://localhost:8000/analysis/0001067983-25-000098/0001067983-26-000012"
  ```

#### `GET /company/{cik}/compare/latest` — Automatic Latest Comparison
Convenience endpoint that automatically resolves and compares the two most recent filings for a given manager CIK.
* **Example**: `curl "http://localhost:8000/company/0001067983/compare/latest"`

---

### 6. AI Summaries & Health Check

#### `POST /api/ai_summary` — AI Portfolio Summary
Accepts portfolio changes payload and generates an executive summary using DeepSeek LLM.
* **Request Body**: `{"new_holdings": [...], "closed_positions": [...], "increased_holdings": [...], "decreased_holdings": [...]}`.

#### `GET /health` — Liveness Probe
Returns `{"status": "ok", "environment": "production"}`. Used by load balancers and container health checks.

---

## 🛠️ Technology Stack

- **Framework**: [FastAPI](https://fastapi.tiangolo.com/)
- **Database**: PostgreSQL (with connection pooling via `psycopg2.pool.ThreadedConnectionPool`)
- **Data Engine**: [Polars](https://pola.rs/) & [Pandas](https://pandas.pydata.org/)
- **ASGI Server**: [Uvicorn](https://www.uvicorn.org/)
- **AI Integration**: [OpenAI Python SDK](https://github.com/openai/openai-python) (DeepSeek backend)
- **Validation**: [Pydantic v2](https://docs.pydantic.dev/)

---

## 🚀 Getting Started

### Prerequisites
- Python 3.10+
- PostgreSQL database populated with SEC 13F filing tables.
- DeepSeek API Key (optional, for `/api/ai_summary`).

### Installation
```bash
git clone https://github.com/JasonLing95/secapi.git
cd secapi
pip install -r requirements.txt
```

### Running Locally
```bash
APP_ENV=development uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

---

## ⚙️ Configuration

Create a `.env` file or export the following environment variables:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `APP_ENV` | `production` | `production` (strict config validation) or `development` |
| `DEBUG` | `false` | Enable verbose error traces |
| `ENABLE_DOCS` | `false` | Enable `/docs` and `/redoc` in production |
| `DB_HOST` | `localhost` | PostgreSQL host |
| `DB_PORT` | `5432` | PostgreSQL port |
| `DB_NAME` | `sec` | Database name |
| `DB_USER` | `postgres` | Database username |
| `DB_PASSWORD` | *(required in prod)* | Database password |
| `DB_POOL_MIN_CONN` | `4` | Minimum database pool connections |
| `DB_POOL_MAX_CONN` | `20` | Maximum database pool connections |
| `DEEPSEEK_API_KEY` | `null` | API key for AI summary endpoint |
| `CORS_ORIGINS` | `""` | Comma-separated allowed frontend origins |

---

## 📚 Interactive API Documentation

Interactive Swagger and ReDoc documentation are generated by FastAPI:
- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

*(In production, enable documentation by setting `ENABLE_DOCS=true`)*
