# Single image used for both the collector and the Dash app — they share
# the same Python deps.  docker-compose.yml picks the entrypoint per service.
#
# Build:    docker compose build
# Run:      docker compose up -d
# Logs:     docker compose logs -f collector
#           docker compose logs -f app

FROM python:3.11-slim

# psycopg2-binary ships its own libs, but build-essential is sometimes
# needed if asyncua/pandas fall back to source builds on the slim base.
# Apt cleanup keeps the image small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps in a separate layer so source changes don't bust the
# dependency cache (much faster rebuilds during dev).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code last so iterating on Python files is cheap.
# .dockerignore excludes .git, __pycache__, logs, etc.
COPY . .

# Default command runs the collector; the `app` service overrides this in
# docker-compose.yml.  `-u` flushes stdout so `docker logs` shows output
# immediately rather than buffering it for minutes at a time.
CMD ["python", "-u", "collector/main.py"]
