FROM python:3.12-slim

RUN useradd --create-home --shell /bin/bash app

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/
COPY static/ /app/static/

# /data is the read-only mount of the ingest data; /db holds the
# annotations SQLite database (its own writable volume).
RUN mkdir -p /data /db && chown -R app:app /data /db /app

USER app

EXPOSE 8091

CMD ["gunicorn", \
     "--bind", "0.0.0.0:8091", \
     "--workers", "2", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
