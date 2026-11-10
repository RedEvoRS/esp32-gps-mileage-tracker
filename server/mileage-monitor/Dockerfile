FROM python:3.12-slim

RUN useradd --create-home --shell /bin/bash app
WORKDIR /app
COPY mileage-monitor/monitor.py mileage-monitor/gelf.py mileage-monitor/prometheus.py mileage-monitor/loki.py mileage-monitor/shared.py mileage-monitor/settings_rules.py /app/
COPY common/ /app/common/
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
RUN mkdir -p /data /state && chown -R app:app /data /state /app
ARG SERVER_VERSION=unversioned
ENV SERVER_VERSION=${SERVER_VERSION}

USER app
CMD ["python", "monitor.py"]
