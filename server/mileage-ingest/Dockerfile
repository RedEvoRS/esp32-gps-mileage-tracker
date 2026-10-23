FROM python:3.12-slim

RUN useradd --create-home --shell /bin/bash app

WORKDIR /app

COPY mileage-ingest/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY mileage-ingest/app.py mileage-ingest/diag_store.py mileage-ingest/reset_high_water.py /app/
COPY common/ /app/common/
ENV PYTHONPATH=/app

RUN mkdir -p /data && chown -R app:app /data /app

ARG SERVER_VERSION=unversioned
ENV SERVER_VERSION=${SERVER_VERSION}

USER app

EXPOSE 8090

# --timeout covers reading the upload, not just handling it. gunicorn's
# default of 30 s is too short for large trips on slow links. A full
# 8 MiB split piece takes ~839 s at 10 kB/s; 900 s leaves a margin for
# handling it. test_upload_limits.py checks the transfer-time margin.
# Keep sync workers: rejected-body deadlines use main-thread POSIX SIGALRM.
# Incomplete headers still need a reverse proxy with an absolute header deadline.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8090", \
     "--workers", "2", \
     "--timeout", "900", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
