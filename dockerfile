FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OPML_PATH=/data/subscriptions.xml \
    ADDED_FILE=/data/added.xml \
    CACHE_FILE=/data/episodes_cache.json \
    MANUAL_DIR=/data/manual \
    DEBUG_SKIPS=1 \
    MAX_WORKERS=1 \
    RATE_LIMIT_DELAY=0.5 \
    RETRY_MAX_ATTEMPTS=3 \
    HOST=0.0.0.0 \
    PORT=5000 \
    THREADS=8

WORKDIR /app
RUN pip install --no-cache-dir flask feedparser requests waitress
COPY random-podcast.py .
VOLUME ["/data"]
EXPOSE 5000
CMD ["python", "random-podcast.py"]
