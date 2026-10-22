FROM python:3.12-slim

RUN useradd --create-home --shell /bin/bash app

WORKDIR /app

COPY mileage-viewer/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY mileage-viewer/app.py /app/
COPY mileage-viewer/geocoding.py mileage-viewer/trip_merges.py mileage-viewer/users.py /app/
COPY mileage-viewer/static/ /app/static/
COPY common/ /app/common/
ENV PYTHONPATH=/app

# /data is the read-only mount of the ingest data; /db holds the
# annotations SQLite database (its own writable volume).
RUN mkdir -p /data /db && chown -R app:app /data /db /app

ARG SERVER_VERSION=unversioned
ENV SERVER_VERSION=${SERVER_VERSION}

USER app

EXPOSE 8091

CMD ["gunicorn", \
     "--bind", "0.0.0.0:8091", \
     "--workers", "2", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
