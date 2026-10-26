# Sethster API server. Build context: this directory (server/).
#   docker build -t sethster-server .            (on the NAS, or add --platform linux/amd64 on an Apple-silicon Mac)
FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY sethster_server ./sethster_server
RUN pip install --no-cache-dir .

# /data  = the project's data/ dir (results, dossiers, wiki, sethster.db, cache/) -- read-write
# /media = the recordings share -- mount READ-ONLY
ENV SETHSTER_DATA=/data \
    MEDIA_ROOT=/media \
    SETHSTER_HOST=0.0.0.0 \
    SETHSTER_PORT=8642 \
    PYTHONUNBUFFERED=1

EXPOSE 8642
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=20s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8642/api/health', timeout=15)" || exit 1

# The server builds the DB on first start if /data/sethster.db is missing.
# Re-import after new research:  docker compose exec sethster python -m sethster_server.importer
CMD ["python", "-m", "sethster_server"]
