# mailer-service — приём писем в очередь (SQLite outbox) + отправка через SMTP-ESP.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# outbox.db лежит на volume /data (DB_PATH=/data/outbox.db в compose).
RUN useradd --system --uid 10001 mailer && mkdir -p /data && chown -R mailer /app /data
USER mailer

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
