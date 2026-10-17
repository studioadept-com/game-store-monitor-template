# A Cloud Run service: each POST /run from Cloud Scheduler runs one monitor check.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY config.json .
COPY store_monitor store_monitor
USER nobody
CMD ["python", "-m", "store_monitor.server"]
