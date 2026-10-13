FROM python:3.12-slim

RUN useradd --create-home --shell /bin/bash app

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/

RUN mkdir -p /data && chown -R app:app /data /app

USER app

EXPOSE 8090

CMD ["gunicorn", \
     "--bind", "0.0.0.0:8090", \
     "--workers", "2", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
