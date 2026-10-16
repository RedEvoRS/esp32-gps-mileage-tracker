FROM python:3.12-slim

RUN useradd --create-home --shell /bin/bash app
WORKDIR /app
COPY mileage-monitor/monitor.py mileage-monitor/gelf.py mileage-monitor/prometheus.py /app/
COPY common/ /app/common/
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
RUN mkdir -p /data /state && chown -R app:app /data /state /app
USER app
CMD ["python", "monitor.py"]
