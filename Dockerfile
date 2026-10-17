# Runs one monitor check and exits; Cloud Scheduler starts it every five minutes.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY config.json .
COPY store_monitor store_monitor
USER nobody
ENTRYPOINT ["python", "-m", "store_monitor"]
