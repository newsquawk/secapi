Here is a README for your FastAPI application, formatted for GitHub.

# SEC Filings Analysis API 📊

This project is a high-performance backend API built with **FastAPI** to serve and analyze SEC 13F filing data. It connects to a PostgreSQL database, performs complex data analysis using **Polars**, and provides structured JSON endpoints for a web frontend.

The API is designed to deliver insights into investment managers' portfolios, track changes over time, and even generate AI-powered summaries of significant activity.

---

## ✨ Features & Endpoints

The API provides a comprehensive set of endpoints to query and analyze filing data:

- **Manager & Filing Data**

  - `GET /managers/`: Retrieves a paginated list of all investment managers.
  - `GET /managers/{cik}`: Fetches detailed information for a single manager by their CIK.
  - `GET /managers/{cik}/filings`: Lists all historical filings for a specific manager.
  - `GET /filings/`: Returns a paginated and sortable list of all recent filings from all managers.
  - `GET /filings/{accession_number}`: Gets metadata for a single filing by its accession number.

- **Detailed Holdings**

  - `GET /holdings/{accession_number}`: Provides detailed holdings for a specific filing, with server-side pagination, searching, and sorting compatible with the DataTables.js library.

- **Portfolio Comparison & Analysis**

  - `GET /analysis/{prev_accession}/{latest_accession}`: The core analysis endpoint. Compares two filings and returns a detailed breakdown of portfolio changes (new, closed, increased, decreased, and unchanged positions) for both common stock and other securities.
  - `GET /company/{cik}/compare/latest`: A convenience endpoint to automatically compare the two most recent filings for a given CIK.

- **AI-Powered Summaries**

  - `POST /api/ai_summary`: Accepts portfolio change data and uses the DeepSeek API to generate a concise, human-readable summary of the most significant changes.

- **Curated "Stories"**

  - `GET /stories/latest/`: A high-level endpoint that automatically finds recent filings, compares them to their predecessors, and identifies the most significant portfolio changes to create a "story" feed.

---

## 🛠️ Technology Stack

- **Framework**: [FastAPI](https://fastapi.tiangolo.com/)
- **Database**: PostgreSQL
- **DB Driver**: [Psycopg2](https://www.psycopg.org/docs/)
- **Data Analysis**: [Polars](https://pola.rs/)
- **Web Server**: [Uvicorn](https://www.uvicorn.org/)
- **AI Integration**: [OpenAI Python SDK](https://github.com/openai/openai-python) (for DeepSeek)
- **Data Validation**: [Pydantic](https://www.google.com/search?q=https://docs.pydantic.dev/)

---

## 🚀 Getting Started

### Prerequisites

- Python 3.8+
- A running PostgreSQL database with the required schema.
- Access to the DeepSeek API (optional, for AI summaries).

### Installation

1.  **Clone the repository:**

    ```bash
    git clone https://github.com/JasonLing95/secapi.git
    cd secapi
    ```

2.  **Install dependencies:**

    ```bash
    pip install -r requirements.txt
    ```

### Configuration

The application is configured using environment variables. You can create a `.env` file in the root directory or export them in your shell.

```
# Runtime
APP_ENV=production                 # default; enforces fail-fast config checks in production
DEBUG=false                         # set "true"/"1" to enable debug mode and stack traces
ENABLE_DOCS=false                   # set "true" to expose /docs, /redoc, /openapi.json
ALLOWED_HOSTS=                      # optional comma-separated Host allowlist

# Database Connection
DB_HOST=localhost
DB_PORT=5432
DB_NAME=sec
DB_USER=postgres
DB_PASSWORD=                        # REQUIRED when APP_ENV=production
DB_CONNECT_TIMEOUT=10               # seconds
DB_SSLMODE=prefer                   # prefer | require | verify-full
DB_KEEPALIVE_IDLE=30                # seconds

# SEC EDGAR Identity (for any direct API calls)
EDGAR_IDENTITY="Your Name or Company your.email@example.com"

# DeepSeek API Key (optional, for AI summaries)
DEEPSEEK_API_KEY="your-deepseek-api-key"

# CORS Origins (comma-separated URLs of your frontend)
CORS_ORIGINS="http://localhost:5000,http://127.0.0.1:5000"
```

### Running the API

Start the server using Uvicorn:

```bash
# Local development (skips production config checks)
APP_ENV=development uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

In production (`APP_ENV=production`, the default), the app **fails fast at startup** if `DB_PASSWORD` is not set, and interactive docs (`/docs`, `/redoc`) are disabled unless `ENABLE_DOCS=true`.

A liveness probe is available at `GET /health` (used by the container `HEALTHCHECK`).

_`main` is the name of your Python file (e.g., `main.py`)._

---

## 📚 API Documentation

Once the server is running, FastAPI automatically generates interactive API documentation. You can access it at:

- **Swagger UI**: [http://localhost:8000/docs](https://www.google.com/search?q=http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](https://www.google.com/search?q=http://localhost:8000/redoc)
