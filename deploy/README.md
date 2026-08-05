# Деплой (systemd)

Два юнита:
- [grosterhit-api.service](grosterhit-api.service) — API (`uvicorn`): фиды, `/cart-ping`, `/esp/webhook`, `/kpi`.
- [grosterhit-workers.service](grosterhit-workers.service) — постоянный процесс воркеров
  (`scripts/worker_loop.py`): Best Offer, Корзина, Постпродажа, атрибуция.

## Установка

1. Разложить проект в `/opt/grosterhit`, создать venv и `.env`:
   ```bash
   cd /opt/grosterhit
   python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cp .env.example .env   # заполнить DATABASE_URL, SMTP_*, интервалы,
                          # CORS_ORIGINS (домены витрины groster.me),
                          # PUBLIC_BASE_URL=https://groster.skypath.fun (домен API — ссылки в письмах)
   ```
2. Создать системного пользователя (без логина):
   ```bash
   sudo useradd --system --home /opt/grosterhit --shell /usr/sbin/nologin grosterhit
   sudo chown -R grosterhit:grosterhit /opt/grosterhit
   ```
3. Поставить юниты (при других путях/юзере — поправить `User`/`WorkingDirectory`/`ExecStart`):
   ```bash
   sudo cp deploy/grosterhit-api.service deploy/grosterhit-workers.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now grosterhit-api grosterhit-workers
   ```
   API слушает `127.0.0.1:8000` — наружу через nginx с TLS (сам TLS не терминирует).

## Эксплуатация
```bash
systemctl status grosterhit-api grosterhit-workers
journalctl -u grosterhit-workers -f         # логи тиков воркеров
journalctl -u grosterhit-api -f             # логи API
sudo systemctl restart grosterhit-workers
sudo systemctl stop grosterhit-workers       # мягкая остановка по SIGTERM
```

## nginx + TLS
[nginx.conf](nginx.conf) — TLS-терминация и прокси на API. API наружу не смотрит.
```bash
sudo cp deploy/grosterhit_proxy.inc /etc/nginx/grosterhit_proxy.inc
sudo cp deploy/nginx.conf /etc/nginx/sites-available/grosterhit
sudo ln -s /etc/nginx/sites-available/grosterhit /etc/nginx/sites-enabled/
sudo certbot certonly --webroot -w /var/www/certbot -d groster.skypath.fun   # TLS-сертификат
sudo nginx -t && sudo systemctl reload nginx
```
Публичный домен API — `groster.skypath.fun` (embed сниппета + ссылки в письмах). Отправка
писем идёт с `mail.groster.me` (DKIM/SPF/DMARC) — это разные домены.

Доступ по эндпоинтам:
- `/cart-ping`, `/trigger.js` — публичные (браузер с витрины: пинг + загрузка сниппета).
- `/unsubscribe` — публичный (ссылка из футера писем, 152-ФЗ).
- `/esp/webhook` — публичный, но ограничить `allow` на IP ESP (в конфиге закомментировано — заполнить).
- `/feeds/*`, `/admin/*`, `/kpi`, `/health` — только внутренняя сеть/VPN (`allow 10.0.0.0/8`).
  Импорт каталога — `/admin/import-xml` (лимит тела 50 МБ). `/demo` наружу не отдаётся (dev-only).

## Заметки
- Рестарт при крахе — `Restart=always` (сам loop от одной ошибки тика не падает).
- Остановка мягкая: loop ловит SIGTERM, закрывает пул БД (проверено, exit 0).
- Интервалы правятся в `.env` без изменения кода; после правки — `restart`.
- API (`uvicorn app.main:app`) — отдельный процесс/юнит; этот файл только про воркеры.
- Альтернатива одному процессу — раздельные cron-джобы на `scripts/run_worker.py`.
