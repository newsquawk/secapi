# SEC Filings Analysis API 📊

A high-performance backend API built with **FastAPI** to serve and analyze SEC Form 13F filing data. It connects to a PostgreSQL database, executes fast data manipulation with **Polars**, and provides structured JSON endpoints for web frontends, market research tools, and institutional flow analysis.

---

## 📑 Table of Contents
- [✨ Key Features & Feeds](#-key-features--feeds)
- [📡 API Endpoints Reference](#-api-endpoints-reference)
  - [1. Activity Feeds & Story Streams](#1-activity-feeds--story-streams)
  - [2. Real-Time Change Feed & Stream (Sync Contract)](#2-real-time-change-feed--stream-sync-contract)
  - [3. Institutional Flow & Market Leaderboards](#3-institutional-flow--market-leaderboards)
  - [4. Manager & Company Endpoints](#4-manager--company-endpoints)
  - [5. Filings & Detailed Holdings](#5-filings--detailed-holdings)
  - [6. Portfolio Comparison & Analysis](#6-portfolio-comparison--analysis)
  - [7. AI Summaries & Health Check](#7-ai-summaries--health-check)
- [🧪 Running Tests](#-running-tests)
- [🗄️ Database Migrations & Integrity Scripts](#️-database-migrations--integrity-scripts)
- [🛠️ Technology Stack](#️-technology-stack)
- [🚀 Getting Started](#-getting-started)
- [⚙️ Configuration](#️-configuration)
- [📚 Interactive API Documentation](#-interactive-api-documentation)

---

## ✨ Key Features & Feeds

- **Real-Time Sync Stream (`/stream` & `/changes`)**: Event-driven LISTEN/NOTIFY SSE doorbell and a source-less, cursor-paginated change feed for downstream ingestion (`content-hub`), delivering complete filings with all holdings embedded.
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
  * `min_value` *(float, optional)*: Filter by minimum absolute dollar value change in **whole dollars ($)**, e.g. `10000000` = $10M+.
  * `limit` *(int, default=10, 1-50)*: Number of fund filings to fetch (not the number of trade rows).
  * `offset` *(int, default=0)*: Number of fund filings to skip.
* **Response envelope**: `{ "activities": [ ... ], "has_next_page": boolean }`. `has_next_page` is `true` when more filings exist beyond the current `limit` / `offset` window.
* **`activities[]` object fields**:

  > 💡 **Consumer quick-reference** — the fields covering the "ticker / name / put-or-call / value / shares" contract are `ticker`, `issuer_name`, `put_or_call`, `current_value` / `previous_value` / `absolute_value_change`, and `current_shares` / `previous_shares` / `change_in_share`.

  | Field | Type | Description |
  | :--- | :--- | :--- |
  | `cik` | string | CIK of the filing investment manager |
  | `company_name` | string | Manager / fund name |
  | `aum` | integer \| null | Manager's assets under management (whole dollars) |
  | `latest_accession_number` | string | Accession number of the latest filing |
  | `previous_accession_number` | string \| null | Accession number of the previous filing |
  | `reporting_period` | date (`YYYY-MM-DD`) | Period of report of the latest filing |
  | `filing_date` | date (`YYYY-MM-DD`) | Date the latest filing was submitted |
  | `form_type` | string \| null | **📌 Filing type** — e.g. `13F-HR` (initial) or `13F-HR/A` (amendment) |
  | `issuer_name` | string | **📌 Name** — underlying issuer name (e.g. `TESLA MOTORS`) |
  | `cusip` | string | CUSIP of the underlying security |
  | `ticker` | string \| null | **📌 Ticker** — resolved from CUSIP; `null` if unmapped |
  | `is_common_stock` | boolean | Always `false` for options; `true` for common stock |
  | `put_or_call` | string \| null | **📌 Contract type** — `Put` / `Call` (or `PUT` / `CALL`) |
  | `change_type` | string | `new`, `closed`, `increased`, or `decreased` |
  | `current_shares` | integer \| null | **📌 Shares** — contracts or shares held in the latest filing |
  | `previous_shares` | integer \| null | Contracts or shares held in the previous filing |
  | `change_in_share` | integer \| null | `current_shares - previous_shares` |
  | `percent_change` | float \| null | Percentage share change vs. previous filing (2 decimals) |
  | `current_value` | integer \| null | **📌 Value** — current position value in **whole dollars ($)** |
  | `previous_value` | integer \| null | Previous position value in **whole dollars ($)** |
  | `absolute_value_change` | integer | `abs(current_value - previous_value)` in **whole dollars ($)** |
  | `current_price_per_share` | float \| null | `current_value / current_shares` (2 decimals) |
  | `previous_price_per_share` | float \| null | `previous_value / previous_shares` (2 decimals) |
  | `weight_pct` | float \| null | **📌 Portfolio weight** — position's % of fund AUM (e.g. `5.4200` = 5.42%) |
  | `value_pct` | float \| null | **📌 Value change** — % change in total position dollar value QoQ |

> 💡 **Normalized Whole Dollars**: All `*_value` fields are standardized to **whole dollars** across all filings via `holdings_normalised`. Filings submitted to EDGAR before Jan 3, 2023 (originally submitted in $1,000s) are automatically scaled by 1,000, while filings on or after Jan 3, 2023 remain in whole dollars. This guarantees that `current_value / current_shares == current_price_per_share` directly without runtime heuristics, preserving accurate prices for penny stocks.

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
        "aum": 123456789000,
        "latest_accession_number": "0001067983-25-000123",
        "previous_accession_number": "0001067983-25-000098",
        "reporting_period": "2025-06-30",
        "filing_date": "2025-08-14",
        "form_type": "13F-HR",
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
        "current_value": 2500000000,
        "previous_value": null,
        "absolute_value_change": 2500000000,
        "current_price_per_share": 250.0,
        "previous_price_per_share": null,
        "weight_pct": 2.025,
        "value_pct": null
      }
    ],
    "has_next_page": false
  }
  ```

#### `GET /activity/latest/v3` — Common Stock Activity Stream
Retrieves a flat list of individual Common Stock position changes across the latest batch of filings. Employs the exact same normalized `HoldingActivity` schema as options, including `weight_pct`, `value_pct`, and `form_type`.
* **Query Parameters**:
  * `limit` *(int, default=3, 1-15)*: Number of company filings to fetch.
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

### 2. Real-Time Change Feed & Stream (Sync Contract)

High-performance event-driven ingestion pipeline designed for downstream consumers (`content-hub`). Delivers filing-level envelopes containing enriched holding activity changes (`activities[]`) without requiring clients to crawl historical data or poll in tight loops.

The feed is **source-less**: each item is one complete 13F filing with **all** of its holdings embedded in `activities[]` (common stock **and** options, every position tagged `is_common_stock` / `put_or_call`). Content Hub owns the slicing, faceting and per-holding explosion. Requires migration `scripts/006_change_cursor.sql`.

#### `GET /changes/head` — Latest Cursor Bookmark
Returns an **opaque cursor** for the newest *settled* 13F filing. Pass it as `cursor` to `GET /changes` to tail live filings from 'now' rather than backfilling all ~288k historical filings.
* **Response Model**: `ChangesHeadResponse`
* **Response**: `{"head_cursor": "MjAyNi0wOS0yM1QxNDowMzoyMi4xMjM0NTZaXzM0MDE5MA=="}`
* **Query Parameters**: `lag_seconds` *(float, default=5)* — hide filings settled less than this many seconds ago.
* **Example**:
  ```bash
  curl "http://localhost:8000/changes/head"
  ```

#### `GET /changes` — Cursor-Paginated Change Feed
Drains changes page-by-page. Keyset-paginated over the composite `(updated_at, filing_id)` cursor: revisions of an already-served filing **re-surface** (their `updated_at` is DB-stamped on update), and a `lag_seconds` high-water mark prevents concurrent out-of-order commits from being skipped.
* **Query Parameters**:
  * `cursor` *(string, optional)*: Opaque cursor from a prior page's `next_cursor`. Omitted → start from the beginning.
  * `limit` *(int, default=10, 1-25)*: Max filings per page. Kept low because each filing embeds **all** its holdings (rely on gzip at the proxy).
  * `lag_seconds` *(float, default=5)*: Hide filings settled less than this many seconds ago.
* **Response Model**: `ChangesResponse`
* **Pagination semantics**: `next_cursor` = the last row's cursor on **any non-empty page** (always resumable); `null` **only** on an empty page. `has_more` = `len(page) == limit` (a separate "more right now" hint).
* **Response Format**:
  ```json
  {
    "items": [
      {
        "filing_id": 288001,
        "accession_number": "0001067983-26-000012",
        "cik": "0001067983",
        "company_name": "BERKSHIRE HATHAWAY INC",
        "form_type": "13F-HR",
        "filing_date": "2026-02-14",
        "period_of_report": "2025-12-31",
        "aum": 299253556246,
        "file_number": "028-12345",
        "filing_directory": "0001067983-26-000012",
        "created_at": "2026-02-14T21:03:11Z",
        "updated_at": "2026-02-14T21:03:11Z",
        "previous_filing_id": 285400,
        "previous_accession_number": "0001067983-25-000098",
        "activities": [
          {
            "issuer_name": "APPLE INC",
            "cusip": "037833100",
            "ticker": "AAPL",
            "is_common_stock": true,
            "put_or_call": null,
            "change_type": "increased",
            "current_shares": 915560382,
            "current_value": 150975906991,
            "current_price_per_share": 164.90,
            "weight_pct": 46.4385,
            "value_pct": 29.81,
            "form_type": "13F-HR"
          }
        ]
      }
    ],
    "next_cursor": "MjAyNi0wMi0xNFQyMTowMzoxMS4wMDAwMDBaXzI4ODA1MA==",
    "has_more": true
  }
  ```
  *(When no filings exist beyond `cursor`, `items` is `[]`, `next_cursor` is `null` and `has_more` is `false`)*.
* **Example**:
  ```bash
  curl "http://localhost:8000/changes?limit=25"
  ```

#### `GET /stream` — Real-Time SSE Doorbell
Persistent Server-Sent Events (SSE) stream backed by Postgres **LISTEN/NOTIFY** (not polling). A DB trigger (migration `006`) fires `pg_notify('filing_changes', …)` on every 13F insert or revision; each uvicorn worker holds a `LISTEN` connection and relays it to connected clients, who then drain `GET /changes` from their own cursor.
* **Headers**: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`
* **Query Parameters**:
  * `once` *(boolean, default=false)*: Emit the connection frame and close (health probes / tests).
  * `replay_latest` *(boolean, default=false)*: Replay the last change signal on connect.
  * `lag_seconds` *(float, default=5)*: Applied to the head reported in the greeting frame.
* **SSE Event Stream Format**:
  ```text
  event: connected
  data: {"head": "MjAyNi0wOS0yM1QxNDowMzoyMi4xMjM0NTZaXzM0MDE5MA=="}

  event: change
  data: {"id": "0001067983-26-000012", "filing_id": 340195, "op": "insert", "ts": "2026-09-23T14:03:22.123456Z"}

  : ping
  ```
* **Example**:
  ```bash
  curl -N "http://localhost:8000/stream"
  ```

---

### 3. Institutional Flow & Market Leaderboards

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

### 4. Manager & Company Endpoints

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

### 5. Filings & Detailed Holdings

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

### 6. Portfolio Comparison & Analysis

#### `GET /analysis/{previous_accession}/{latest_accession}` — Two-Filing Portfolio Comparison
Compares two filings for a manager and returns a comprehensive breakdown of changes:
* **Common Stock**: `new_holdings`, `closed_positions`, `increased_holdings`, `decreased_holdings`, `unchanged_holdings`.
* **Other Securities / Options**: `new_other_securities`, `closed_other_securities`, `increased_other_securities`, `decreased_other_securities`, `unchanged_other_securities`.
* **Response Envelope & Metadata**:
  Includes top-level `metadata` object (`cik`, `company_name`, `ai_summary`, `latest_filing`, `previous_filing`, `amendment_used`) and:
  * `truncated` *(boolean)*: `true` if either filing exceeded the 25,000-holding query safety limit (top 25k ordered by dollar value), alerting clients that only partial holdings are returned for mega-funds; `false` otherwise.
* **HTTP Caching**: Employs deterministic SHA-256 ETags and `Cache-Control: public, max-age=86400, stale-while-revalidate=604800, immutable` for browser and proxy caching across restarts.
* **Example**:
  ```bash
  curl "http://localhost:8000/analysis/0001067983-25-000098/0001067983-26-000012"
  ```

#### `GET /company/{cik}/compare/latest` — Automatic Latest Comparison
Convenience endpoint that automatically resolves and compares the two most recent filings for a given manager CIK. Employs the identical response schema, caching headers, and `truncated` metadata safety flag as `/analysis/...`.
* **Example**: `curl "http://localhost:8000/company/0001067983/compare/latest"`

---

### 7. AI Summaries & Health Check

#### `POST /api/ai_summary` — AI Portfolio Summary
Accepts portfolio changes payload and generates an executive financial summary using DeepSeek LLM.
* **Request Body**: `{"new_holdings": [...], "closed_positions": [...], "increased_holdings": [...], "decreased_holdings": [...]}`.
* **Persistent Two-Tier Caching**: Results are deterministically hashed and stored in PostgreSQL (`ai_summaries`) and an in-memory LRU cache with auto-eviction (`popitem`). Repeated requests return in **<10ms** across all server workers without re-calling the LLM.

#### `GET /health` — Liveness Probe
Returns `{"status": "ok"}`. Used by load balancers and container health checks.

---

## 🧪 Running Tests

The test suite validates data normalization accuracy against historical stock market closing prices and guards against output regressions:

```bash
# Run all unit and regression tests
./env/bin/python -m unittest discover tests -v
```

### Test Suites:
1. **Value Normalization Accuracy (`tests/test_value_normalization_accuracy.py`)**:
   - Cross-checks Apple (AAPL, CUSIP `037833100`) implied share prices against NYSE closing prices across 2022 and 2023.
   - Verifies portfolio AUM stability (`AUM / SUM(normalised_value) ≈ 1.0`).
   - Verifies that post-2023 sub-$1 penny stocks (e.g. $0.44/share) are preserved in whole dollars and not inflated.
2. **Characterization Regression (`tests/test_characterization.py`)**:
   - Verifies live query outputs against baseline snapshots (`tests/snapshots/baseline_current.json`) across 4 test cohorts (Pre-2023, Penny Stocks, Post-2023 Standard, Options) to guarantee zero unwanted output drift.
3. **Change Feed & Stream (`tests/test_sync_endpoints.py`)**:
   - Unit-tests the opaque `(updated_at, filing_id)` cursor (round-trip, ordering, invalid-token rejection) and validates `GET /changes/head`, `GET /changes` cursor-walk draining to `next_cursor: null`, and persistent `/stream` SSE connection frames.
4. **Flow Top Changes & Options (`tests/test_flow_top_changes.py`)**:
   - Validates uppercase CUSIP uniqueness, sort-by validation, and candidate pushdown filtering for options activity.

---

## 🗄️ Database Migrations & Integrity Scripts

Located in [`scripts/`](scripts/):
* **CUSIP Deduplication & Normalization (`scripts/deduplicate_issuers.py`)**:
  - Batched migration runner that merges legacy duplicate lowercase/uppercase issuer rows, repoints referencing `holdings` rows using indexed lookups without table locks, and uppercases solitary CUSIPs.
  - Enforces database constraint `chk_issuers_cusip_upper` (`cusip IS NULL OR cusip = UPPER(cusip)`) and unique index `idx_issuers_cusip_upper ON issuers (UPPER(cusip))`.
  - Supports `--dry-run` and configurable `--batch-size`.
* **SQL Schema Reference (`scripts/005_deduplicate_issuers.sql`)**:
  - DDL reference for `holdings_normalised`, CUSIP uppercase constraints, and indexes.
* **Change Cursor (`scripts/006_change_cursor.sql`)**:
  - Establishes the monotonic change cursor for the Content Hub feed: a BEFORE trigger that DB-stamps `filings.updated_at` on every insert/update (so revisions re-surface), a backfill of historical rows, an AFTER trigger emitting `pg_notify('filing_changes', …)` on 13F writes, and the `(updated_at, filing_id)` keyset index. Run manually, off-peak; idempotent.

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
| `RATE_LIMIT` | `120/minute` | Default per-client rate limit |
| `CORS_ORIGINS` | `""` | Comma-separated allowed frontend origins |

---

## 📚 Interactive API Documentation

Interactive Swagger and ReDoc documentation are generated by FastAPI:
- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

*(In production, enable documentation by setting `ENABLE_DOCS=true`)*
