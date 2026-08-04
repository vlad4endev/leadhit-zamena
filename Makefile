# Dev-команды. Требуется локальный PostgreSQL и Python 3.9+ (прод — 3.11+).
PY := ./.venv/bin/python
DB := grosterhit_dev

.PHONY: venv db reset seed run workers test worker-postsale worker-best-offer worker-cart attribution

venv:                      ## Создать venv и поставить зависимости
	python3 -m venv .venv && ./.venv/bin/pip install -q -r requirements.txt
	@test -f .env || cp .env.example .env

db:                        ## Создать БД и применить схему
	createdb $(DB) 2>/dev/null || true
	psql -q -d $(DB) -f db/schema.sql

reset:                     ## Пересоздать БД с нуля
	dropdb --if-exists $(DB) && createdb $(DB) && psql -q -d $(DB) -f db/schema.sql

seed:                      ## Загрузить тестовые данные (API должен быть запущен)
	$(PY) scripts/seed.py

run:                       ## Поднять API (фиды, /cart-ping, /esp/webhook, /kpi)
	./.venv/bin/uvicorn app.main:app --reload

workers:                   ## Поднять постоянный процесс воркеров (все сценарии)
	$(PY) scripts/worker_loop.py

test:                      ## Self-check чистой логики всех сервисов
	$(PY) -m app.postsale
	$(PY) -m app.best_offer
	$(PY) -m app.cart
	$(PY) -m app.analytics

worker-postsale:    ; $(PY) scripts/run_worker.py postsale
worker-best-offer:  ; $(PY) scripts/run_worker.py best_offer
worker-cart:        ; $(PY) scripts/run_worker.py cart
attribution:        ; $(PY) scripts/run_worker.py attribution
