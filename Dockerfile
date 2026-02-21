FROM python:3.12-slim AS base

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY pyproject.toml .
COPY src/ src/

# Optional config files; create data dir for SQLite DBs
COPY projects.yaml* ./
RUN mkdir -p data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/api/status', timeout=8).raise_for_status()"

CMD ["python", "-m", "src.main", "serve"]
