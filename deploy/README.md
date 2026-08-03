# Деплой (systemd)

Два юнита:
- [leadhit-api.service](leadhit-api.service) — API (`uvicorn`): фиды, `/cart-ping`, `/esp/webhook`, `/kpi`.
- [leadhit-workers.service](leadhit-workers.service) — постоянный процесс воркеров
  (`scripts/worker_loop.py`): Best Offer, Корзина, Постпродажа, атрибуция.

## Установка

1. Разложить проект в `/opt/leadhit`, создать venv и `.env`:
   ```bash
   cd /opt/leadhit
   python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cp .env.example .env   # заполнить DATABASE_URL, SMTP_*, интервалы
   ```
2. Создать системного пользователя (без логина):
   ```bash
   sudo useradd --system --home /opt/leadhit --shell /usr/sbin/nologin leadhit
   sudo chown -R leadhit:leadhit /opt/leadhit
   ```
3. Поставить юниты (при других путях/юзере — поправить `User`/`WorkingDirectory`/`ExecStart`):
   ```bash
   sudo cp deploy/leadhit-api.service deploy/leadhit-workers.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now leadhit-api leadhit-workers
   ```
   API слушает `127.0.0.1:8000` — наружу через nginx с TLS (сам TLS не терминирует).

## Эксплуатация
```bash
systemctl status leadhit-api leadhit-workers
journalctl -u leadhit-workers -f         # логи тиков воркеров
journalctl -u leadhit-api -f             # логи API
sudo systemctl restart leadhit-workers
sudo systemctl stop leadhit-workers       # мягкая остановка по SIGTERM
```

## nginx + TLS
[nginx.conf](nginx.conf) — TLS-терминация и прокси на API. API наружу не смотрит.
```bash
sudo cp deploy/leadhit_proxy.inc /etc/nginx/leadhit_proxy.inc
sudo cp deploy/nginx.conf /etc/nginx/sites-available/leadhit
sudo ln -s /etc/nginx/sites-available/leadhit /etc/nginx/sites-enabled/
sudo certbot certonly --webroot -w /var/www/certbot -d mail.groster.me   # TLS-сертификат
sudo nginx -t && sudo systemctl reload nginx
```
Доступ по эндпоинтам:
- `/cart-ping` — публичный (браузер).
- `/esp/webhook` — публичный, но ограничить `allow` на IP ESP (в конфиге закомментировано — заполнить).
- `/feeds/*`, `/kpi`, `/health` — только внутренняя сеть/VPN (`allow 10.0.0.0/8`).

## Заметки
- Рестарт при крахе — `Restart=always` (сам loop от одной ошибки тика не падает).
- Остановка мягкая: loop ловит SIGTERM, закрывает пул БД (проверено, exit 0).
- Интервалы правятся в `.env` без изменения кода; после правки — `restart`.
- API (`uvicorn app.main:app`) — отдельный процесс/юнит; этот файл только про воркеры.
- Альтернатива одному процессу — раздельные cron-джобы на `scripts/run_worker.py`.
