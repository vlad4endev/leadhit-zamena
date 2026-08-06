# Образ приложения GrosterHit — общий для API (uvicorn) и воркеров (worker_loop.py).
# Команду задаёт docker-compose (api / workers). asyncpg и uvicorn[standard] ставятся
# из wheel'ов cp311 — компилятор не нужен, поэтому slim без build-essential.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
# Терпеливый pip + опциональное зеркало: если pypi.org недоступен со сборочного хоста
# (частая картина в РФ-сети — соединение висит на чтении), собрать через зеркало:
#   docker compose build --build-arg PIP_INDEX_URL=https://mirror.yandex.ru/mirrors/pypi/simple/
ARG PIP_INDEX_URL=
RUN pip install --no-cache-dir --timeout 120 --retries 10 \
      ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY db ./db

# Непривилегированный пользователь.
RUN useradd --system --uid 10001 grosterhit && chown -R grosterhit /app
USER grosterhit

EXPOSE 8000

# По умолчанию — API. Воркеры переопределяют command в compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
