# ──────────────────────────────────────────────────────────────────────────────
# SG Internship Aggregator – Dockerfile
#
# Multi-stage build:
#   1. builder  – installs Python deps into a venv
#   2. runtime  – installs Playwright browsers, copies venv + source
#
# Build:  docker build -t sg-internship .
# Run:    docker run -p 8000:8000 sg-internship
# ──────────────────────────────────────────────────────────────────────────────

# ── Stage 1: build dependencies ───────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools needed for some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python -m venv /venv && \
    /venv/bin/pip install --upgrade pip && \
    /venv/bin/pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="SG Internship Aggregator" \
      description="Aggregates Singapore internship postings from 4 portals"

# OS-level dependencies required by Playwright's Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium system libraries
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libwayland-client0 \
    # Fonts for page rendering
    fonts-liberation \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the venv from builder stage
COPY --from=builder /venv /venv

# Make venv the default Python
ENV PATH="/venv/bin:$PATH"

# Install Playwright Chromium browser inside the image
RUN playwright install chromium

# Copy application source
COPY . .

# Create directories for persistent data and logs
RUN mkdir -p /app/data /app/logs

# Environment defaults (override in docker-compose or with -e flags)
ENV HOST=0.0.0.0 \
    PORT=8000 \
    DB_PATH=/app/data/jobs.db \
    LOG_FILE=/app/logs/scraper.log \
    LOG_LEVEL=INFO \
    SCRAPE_INTERVAL_HOURS=6

EXPOSE 8000

# Healthcheck – polls the stats endpoint every 30s
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/stats')" || exit 1

CMD ["python", "api.py"]
