FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/New_York \
    SOURCE_DB=/app/data/jobs.db \
    STATE_DB=/app/data/state.db \
    BACKUP_DIR=/app/backups \
    PORT=8000

WORKDIR /app

# tzdata provides the IANA database used for America/New_York.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY nybackup ./nybackup
COPY templates ./templates
COPY static ./static
COPY tools ./tools
COPY sample ./sample
COPY run.py ./

RUN mkdir -p /app/data /app/backups \
    && useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

VOLUME ["/app/data", "/app/backups"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8000')).read()"

CMD ["python", "run.py"]
