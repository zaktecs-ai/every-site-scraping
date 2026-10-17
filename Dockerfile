FROM python:3.12-slim

LABEL org.opencontainers.image.title="every-site-scraping"
LABEL org.opencontainers.image.description="Parallel Python scraper + crawler: any URL list into a structured dataset, crash-safe."
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches across code edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt psutil

COPY . .

# Run unprivileged: a scraper has no reason to be root.
RUN useradd --create-home --uid 10001 scraper \
 && mkdir -p /data && chown -R scraper:scraper /app /data
USER scraper
WORKDIR /data

ENTRYPOINT ["python", "/app/cli.py"]
CMD ["--help"]
