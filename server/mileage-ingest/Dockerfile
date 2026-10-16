FROM python:3.12-slim

RUN useradd --create-home --shell /bin/bash app

WORKDIR /app

COPY mileage-ingest/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY mileage-ingest/app.py mileage-ingest/diag_store.py /app/
COPY common/ /app/common/
ENV PYTHONPATH=/app

RUN mkdir -p /data && chown -R app:app /data /app

USER app

EXPOSE 8090

# --timeout covers reading the upload, not just handling it. gunicorn's
# default of 30 s killed a full trip partition's file (~2.4 MB) sent at
# 50 kB/s; 300 s lets that file through down to ~8 kB/s, a weak signal
# at the edge of the driveway. test_upload_limits.py checks the margin.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8090", \
     "--workers", "2", \
     "--timeout", "300", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
