# Crawler (Layer 1) — serves the Crawler API (8099) or the Ingest dashboard (9090).
# Playwright base image ships Chromium + all OS deps so render_js works in-container.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt playwright && \
    playwright install chromium

COPY . .

# Which service to run is chosen by the compose command (serve | crawler-api).
EXPOSE 8099 9090
CMD ["python", "run.py", "crawler-api"]
