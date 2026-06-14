# =============================================================================
# JK Tyre PCR planning + simulation API
# =============================================================================
# Exposes TWO endpoints on the same Flask app (port 5001):
#   POST /app/v1/jkt/planning-scheduling/plan/generate-plan         (jkt_* tables)
#   POST /app/v1/jkt/planning-scheduling/simulation/generate-plan   (jkt_sim_* tables)
#
# Multi-stage not needed — pure-Python wheels keep the slim base tiny enough.
# Single-worker gunicorn matches the in-process threading.Lock in
# schedule_route.py; do NOT bump --workers without first refactoring the
# scheduler's Config class to not use process-global state. (The same lock
# serializes planning AND simulation runs — they share the legacy Config.)
# =============================================================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install Python deps first (better layer caching — only re-runs if requirements.txt changes).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source. .dockerignore filters out junk.
COPY . .

# Pipeline writes intermediate Excel files to /app/output. Make sure it exists
# even if .dockerignore wiped it.
RUN mkdir -p /app/output /app/input

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://localhost:5001/app/v1/jkt/planning-scheduling/health', timeout=3).status == 200 else 1)" \
    || exit 1

# --timeout 600 lets the LP solver complete (default 30s would kill it).
# --workers 1 because of the in-process Config-mutation lock; multi-worker would race.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5001", \
     "--workers", "1", \
     "--threads", "4", \
     "--timeout", "600", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
