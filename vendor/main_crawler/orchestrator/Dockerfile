# Orchestrator (Layer 1.5) — source catalog, job generation, scheduler + the unified control panel.
# It drives the crawler worker over HTTP only (no browser/Playwright), so this stays a slim image.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# EDITABLE install on purpose: it keeps the package rooted at /app/src, so main.py's
# Path(__file__).parent / "static" / "index.html" resolves without any package-data config.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --upgrade pip && pip install -e .

COPY seed_data ./seed_data
# Self-checks ship WITH the image so `setup.sh --test` can verify a deployed container rather than
# only the tree it was built from. They are standalone scripts (no pytest), a few KB total.
COPY test_api_smoke.py test_scheduler.py test_tiering.py ./
# The pytest suite ships too, for the same reason — verifying the tree you BUILT proves nothing
# about the container you deployed. pytest itself is deliberately NOT installed: it is a build-time
# dependency, not a runtime one. To run these against a live container:
#     docker exec <c> sh -c 'pip install -q pytest && cd /app && python -m pytest tests -q'
COPY tests ./tests

EXPOSE 8090
# ONE worker deliberately: the scheduler loop runs IN-PROCESS, so multiple workers would
# double-dispatch every due job.
CMD ["uvicorn", "mallory_orchestrator.main:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
