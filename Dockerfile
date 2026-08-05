# Образ приложения GrosterHit — общий для API (uvicorn) и воркеров (worker_loop.py).
# Команду задаёт docker-compose (api / workers). asyncpg и uvicorn[standard] ставятся
# из wheel'ов cp311 — компилятор не нужен, поэтому slim без build-essential.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY db ./db

# Непривилегированный пользователь.
RUN useradd --system --uid 10001 grosterhit && chown -R grosterhit /app
USER grosterhit

EXPOSE 8000

# По умолчанию — API. Воркеры переопределяют command в compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
