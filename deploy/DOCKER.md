# Деплой в Docker (docker compose)

Стек: `db` (PostgreSQL + volume), `api` (uvicorn), `workers` (worker_loop), `mailer`
(отправка писем), `nginx` (TLS-терминация + прокси). Определён в [../docker-compose.yml](../docker-compose.yml).

## 1. Подготовка
```bash
cd /opt/grosterhit                     # проект на сервере
cp .env.example .env                   # заполнить секреты (см. ниже)
```
Заполнить в `.env` как минимум:
- `POSTGRES_PASSWORD` — пароль контейнерной БД (обязателен).
- `CORS_ORIGINS=https://groster.me,https://www.groster.me` — домены витрины.
- `PUBLIC_BASE_URL=https://groster.skypath.fun` — домен API (ссылки в письмах, embed).
- Почта: сервис `mailer` на **host-network** (на VPS bridge часто не ходит в SMTP).
  В `.env` обязателен `MAILER_HOST_IP` — IP моста `br-*` (`ip -br a | grep '^br-'`,
  обычно `172.18.0.1`). SMTP: `SMTP_*` или админка **Настройки → Почта**
  (порт **465** = SSL). Без SMTP — dev-лог. См. [MAIL_DNS.md](MAIL_DNS.md).
- `MAILER_SERVICE_TOKEN` — общий секрет app↔mailer (compose прокинет его как `API_TOKEN`).
- Снаружи закройте порт 8080 на eth0; с docker-моста разрешите
  (`ufw allow from 172.18.0.0/16 to any port 8080`).

`DATABASE_URL` задаёт compose. `MAILER_SERVICE_URL` = `http://host.docker.internal:8080`.

## 2. TLS-сертификат (на хосте, один раз)
nginx-контейнер монтирует серты с хоста (`/etc/letsencrypt`). Выпуск — хостовым certbot:
```bash
sudo certbot certonly --webroot -w /var/www/certbot -d groster.skypath.fun
```
DNS `groster.skypath.fun` должен указывать на сервер. Авто-продление — хостовым `certbot renew`
(в cron/timer); nginx перечитает серты при `docker compose exec nginx nginx -s reload`.

## 3. Запуск
```bash
docker compose up -d --build
docker compose ps
```
Схема БД применяется автоматически при первом старте (пустой volume `pgdata`).

## 4. Проверка
```bash
curl -s https://groster.skypath.fun/track.js | head -1       # единый тег витрины
curl -s https://groster.skypath.fun/trigger.js | head -1     # сниппет корзины
curl -s https://groster.skypath.fun/wheel.js | head -1       # виджет колеса
curl -s https://groster.skypath.fun/health                   # 404 снаружи — так и задумано
docker compose exec api python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/health').read())"
```

## 5. Админка / фиды / импорт — доступ по SSH-туннелю
Наружу служебные эндпоинты НЕ публикуются (безопасность). `api` слушает на хосте
`127.0.0.1:${API_HOST_PORT:-8000}` (по умолчанию 8000; смените в `.env`, если порт занят):
```bash
ssh -L 8000:127.0.0.1:8000 <server>       # с рабочей машины (правый порт = API_HOST_PORT)
# затем в браузере: http://localhost:8000/admin  (импорт каталога — «Импорт из файла»)
```

## 6. Тестовые данные (dev/staging)
```bash
docker compose exec api python scripts/seed.py http://localhost:8000
```

## Эксплуатация
```bash
docker compose logs -f workers            # тики воркеров (в т.ч. корзина)
docker compose logs -f api mailer
docker compose restart workers
docker compose down                       # остановить (volume с данными сохраняется)
docker compose pull && docker compose up -d --build   # обновление
```

## Обновление кода на проде (за NPM)
```bash
cd /opt/grosterhit && git pull && git log --oneline -1     # убедиться, что коммит приехал
ls db/migrations                                           # появились новые файлы — применить (ниже)
# api + workers + mailer: mailer на host-network, без --force-recreate останется старый env/сеть
docker compose up -d --build --force-recreate api workers mailer
docker compose -f docker-compose.edge.yml up -d --force-recreate edge   # конфиг смонтирован файлом
bash deploy/check-mail.sh                                  # SMTP + api→mailer + callback
```
Миграции применяются **после `git pull`** (до него файла на сервере просто нет) и до пересборки;
все они идемпотентны, повтор безопасен:
```bash
docker compose exec -T db psql -U grosterhit -d grosterhit < db/migrations/003_script_hits.sql
docker compose exec -T db psql -U grosterhit -d grosterhit < db/migrations/004_image_proxy.sql
docker compose exec -T db psql -U grosterhit -d grosterhit < db/migrations/005_drop_fake_image_urls.sql
docker compose exec -T db psql -U grosterhit -d grosterhit < db/migrations/006_drop_artikul_image_urls.sql
docker compose exec -T db psql -U grosterhit -d grosterhit < db/migrations/007_activity_log.sql
docker compose exec -T db psql -U grosterhit -d grosterhit < db/migrations/008_email_log_body.sql
```
Признак, что обновление реально применилось: в сборке `COPY app ./app` **без** `CACHED`, новый sha
образа и `Recreated`/`Started` у контейнеров. Если `CACHED` и `Running` — `git pull` не принёс
изменений (типовая причина: коммиты не запушены в `origin`), пересборка тут не поможет.

### 502 сразу после обновления
Первое, что проверить — жив ли сам api (он слушает loopback хоста):
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/health   # 200 → приложение работает
docker compose logs --tail=40 api                                        # иначе смотреть причину здесь
```
Если api отвечает 200, а снаружи 502 — протух адрес апстрима в edge: nginx резолвит имя
`api` один раз при старте, а `up -d --build api` создаёт контейнер с новым IP. Лечится
пересозданием edge:
```bash
docker compose -f docker-compose.edge.yml up -d --force-recreate edge
```
Конфиги в репозитории уже резолвят имя на каждый запрос (`resolver 127.0.0.11` + переменная
в `proxy_pass`), так что после разового обновления edge этот 502 больше не возникает.

**502 только на одном пути (например `/track.js`), а с `?x=1` тот же путь отдаёт 200** — это
NPM закэшировал ошибку: у прокси-хоста включён «Cache Assets», а он кэширует ответы по
расширению (`.js`), включая 502, пока приложение лежало. Витрина этого не замечает (тег
грузится с `?ver=`), но самопроверка в админке будет показывать файл недоступным. Лечение —
снять «Cache Assets» у хоста в UI NPM либо очистить его кэш и перезапустить контейнер NPM.

## Заметки
- **`--remove-orphans` не использовать.** Базовый стек и edge — два compose-файла одного
  проекта, поэтому каждый считает контейнеры другого «orphan» и предупреждает об этом.
  Предупреждение безвредно, а вот флаг из подсказки снесёт живые контейнеры: запуск
  базового файла с ним удалит `edge`, запуск edge-файла — `api`, `workers`, `db`.
- **Новый публичный роут → правка edge-конфига.** Наружу отдаётся whitelist точных путей
  (`deploy/nginx.npm.conf` за NPM, `deploy/nginx.docker.conf` без него), остальное — 404. Добавили
  роут в приложение — добавьте `location =` в конфиг, иначе снаружи 404 при живом роуте внутри.
  Отличить легко: edge отвечает HTML и `server: openresty`, приложение — JSON `{"detail":...}`.
- **Схема при обновлениях**: initdb-скрипт срабатывает только на пустом `pgdata`. Изменения схемы
  после первого запуска применять отдельно (миграция/`psql`), автоперезаливки нет. Миграции
  лежат в `db/migrations/` и идемпотентны — применяются так:
  ```bash
  docker compose exec -T db psql -U grosterhit -d grosterhit < db/migrations/003_script_hits.sql
  docker compose exec -T db psql -U grosterhit -d grosterhit < db/migrations/004_image_proxy.sql
  docker compose exec -T db psql -U grosterhit -d grosterhit < db/migrations/005_drop_fake_image_urls.sql
  docker compose exec -T db psql -U grosterhit -d grosterhit < db/migrations/006_drop_artikul_image_urls.sql
  docker compose exec -T db psql -U grosterhit -d grosterhit < db/migrations/007_activity_log.sql
  docker compose exec -T db psql -U grosterhit -d grosterhit < db/migrations/008_email_log_body.sql
  ```
  Без `003` индикатор связи в админке останется в состоянии «тег не грузился» (таблицы нет —
  счётчик молча не пишется), остальное работает. Без `004`–`006` и без `location /img/` в
  edge-конфиге фото товаров в каталоге и письмах останутся битыми (неверное расширение / 404).
  Без `007` вкладка «Настройки → Логи» покажет ошибку загрузки (таблицы `activity_log` нет).
  Без `008` в журнале писем нельзя открыть HTML-снимок письма (колонок `subject`/`html` нет).
- **Бэкап БД**: `docker compose exec db pg_dump -U grosterhit grosterhit > backup.sql`.
- **Внешняя БД вместо контейнера**: убрать сервис `db` и задать `DATABASE_URL` на внешний Postgres.
- **Client IP**: под Docker nginx видит IP docker-шлюза, не клиента. Поэтому admin/feeds закрыты
  на уровне маршрутизации (не отдаются наружу), а вебхук ESP аутентифицируется в приложении.
- **Почта стабильно (чеклист)**: mailer на **host-network**; api/workers →
  `http://host.docker.internal:8080`; `MAILER_HOST_IP` = IP `br-*` (не docker0);
  ufw: allow с `172.16.0.0/12` на 8080; eth0:8080 закрыт снаружи; `API_HOST_PORT`
  совпадает с `CALLBACK_URL`. Одна строка `MAILER_HOST_IP` в `.env`.

  ```bash
  bash deploy/check-mail.sh                 # единая проверка контура
  ```

  Если check падает:

  ```bash
  ip -br a | grep '^br-'                    # → 172.18.0.1
  # одна строка MAILER_HOST_IP в .env
  ufw allow from 172.16.0.0/12 to any port 8080 proto tcp
  iptables -C INPUT -i eth0 -p tcp --dport 8080 -j DROP 2>/dev/null || \
    iptables -I INPUT -i eth0 -p tcp --dport 8080 -j DROP
  docker compose up -d --build --force-recreate api workers mailer
  bash deploy/check-mail.sh
  ```

  Типичные симптомы → причина:
  - `Network is unreachable` / SMTP timeout из mailer → не host-network (старый compose/ветка)
  - `Temporary failure in name resolution` / `http://mailer:8080` → нужен host.docker.internal
  - api `Connection refused` на :8080 → ufw или неверный `MAILER_HOST_IP`
  - `[callback-fail] Connection refused` → api не на `127.0.0.1:$API_HOST_PORT`
  - SMTP `535` → новый пароль приложения Яндекса (не обычный пароль ящика)

  Самопроверка в админке («Интеграция») тоже проверяет mailer/SMTP/callback.
