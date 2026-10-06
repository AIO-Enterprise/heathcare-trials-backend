FROM python:3.12-slim

WORKDIR /app

# Install TrueType fonts for Pillow compositor
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    fonts-liberation \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .
COPY skills/ /skills/

# Run as non-root user — UID/GID 1000 matches EFS access point POSIX user
RUN groupadd -g 1000 appuser \
    && useradd -m -u 1000 -g 1000 appuser \
    && mkdir -p /app/uploads /app/outputs /app/static /db \
    && chown -R appuser:appuser /app /db /skills
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

# Single worker — SQLite does not support concurrent multi-process writes.
# Multiple workers cause silent "database is locked" failures where the route
# returns 200 but the commit is rolled back. Switch to PostgreSQL before
# scaling to multiple workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
