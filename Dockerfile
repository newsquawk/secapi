FROM python:3.11.4-slim

WORKDIR /app

# Create a dedicated non-root user to run the application.
RUN groupadd --system --gid 1001 appuser \
    && useradd --system --uid 1001 --gid appuser --no-create-home appuser

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py sec_models.py config.py database.py utils.py /app/
COPY routers/ ./routers/
COPY data/ ./data/
COPY scripts/ ./scripts/
COPY tests/ ./tests/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8000

# The app is read-only at runtime; own /app so the non-root user can use it.
RUN chown -R appuser:appuser /app

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "3", "--forwarded-allow-ips", "*"]
