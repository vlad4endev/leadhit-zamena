# Переезд на новый сервер (чистый VPS, тот же docker-стек)

Сценарий: локальный сервер → облачный VPS, **новый поддомен** для API, данные БД
переносим целиком через `pg_dump`. TLS терминирует nginx-контейнер (`--profile standalone`),
сертификат выпускает хостовый certbot.

Переносится ровно три вещи: **репозиторий**, **`.env`**, **дамп БД** (+ два каталога с
файлами, см. шаг 5). Образы, `pgdata` как файлы и `.venv` не переносятся — пересобираются.

Обозначения: `OLD` — текущий сервер, `NEW` — новый VPS, `api.example.com` — новый поддомен.

---

## 0. До окна переключения (без даунтайма)

### 0.1 Что менять на стороне подрядчиков — узнать заранее
- **1С**: интеграция работает по *pull* (наш сервер сам ходит в `ONEC_BASE_URL`). Если на
  стороне 1С стоит allowlist по IP — добавить IP `NEW` **до** переключения. Токен тот же.
- **Витрина**: тег и сниппет корзины грузятся с `PUBLIC_BASE_URL`. Новый домен → на витрине
  надо поменять `src` (шаг 7). Согласовать окно с тем, кто правит шаблон магазина.
- **ESP/SMTP**: если провайдер привязывает отправку к IP — добавить IP `NEW`. SPF/DKIM/DMARC
  относятся к почтовому домену, а не к серверу приложения — их менять не нужно.

### 0.2 DNS
A-запись `api.example.com` → IP `NEW`. TTL заранее опустить до 300 c.
```bash
dig +short api.example.com     # должен вернуть IP NEW
```

### 0.3 Подготовить NEW
```bash
# Docker + compose plugin (Debian/Ubuntu)
curl -fsSL https://get.docker.com | sh
apt-get install -y git certbot

# Firewall: наружу только SSH и HTTP(S). Postgres и api наружу не смотрят вообще
ufw allow 22,80,443/tcp && ufw enable
```

### 0.4 Код и конфиг на NEW
```bash
git clone <repo> /opt/grosterhit && cd /opt/grosterhit
git log --oneline -1                       # тот же коммит, что на OLD
```
Скопировать `.env` с OLD (в git его нет) и поправить под новый домен:
```bash
scp OLD:/opt/grosterhit/.env /opt/grosterhit/.env
```
| Ключ | Что поставить |
|---|---|
| `PUBLIC_BASE_URL` | `https://api.example.com` — ссылки в письмах, отписка, embed |
| `CORS_ORIGINS` | домены **витрины** (не меняются: `https://groster.me,https://www.groster.me`) |
| `SHOP_URL` | домен витрины, не меняется |
| `POSTGRES_PASSWORD`, `MAILER_SERVICE_TOKEN`, `SESSION_SECRET`, `ADMIN_PASSWORD` | перенести как есть либо сгенерировать заново (переезд — удобный повод ротировать) |
| `API_HOST_PORT` | на чистом VPS 8000 свободен — можно убрать переопределение |

Подставить новый домен в конфиг nginx:
```bash
sed -i 's/groster\.skypath\.fun/api.example.com/g' deploy/nginx.docker.conf
```

### 0.5 Сертификат (до старта контейнеров — порт 80 должен быть свободен)
```bash
mkdir -p /var/www/certbot
certbot certonly --standalone -d api.example.com
# продление: systemd-таймер certbot ставится пакетом; после обновления серта —
# docker compose exec nginx nginx -s reload  (в cron раз в неделю)
```

### 0.6 Репетиция: поднять стек с тестовым дампом
```bash
docker compose --profile standalone up -d --build
docker compose ps
```
Схема применяется автоматически на пустом `pgdata`. Прогнать пробное восстановление
(шаг 3) и чек-лист (шаг 9) на копии данных — до реального окна.

---

## Окно переключения (≈10–15 минут)

## 1. Остановить запись на OLD
```bash
# на OLD: воркеры не должны писать в БД и слать письма во время дампа
docker compose stop workers api
```

## 2. Дамп
`--clean --if-exists` обязателен: на NEW схема уже создана initdb-скриптом, без этих флагов
восстановление упадёт на `relation already exists`.
```bash
# на OLD
docker compose exec -T db pg_dump -U grosterhit --clean --if-exists grosterhit > /tmp/gh.sql
scp /tmp/gh.sql NEW:/tmp/gh.sql
```

## 3. Восстановление на NEW
```bash
docker compose up -d db                       # если стек ещё не поднят
docker compose exec -T db psql -U grosterhit -d grosterhit < /tmp/gh.sql
docker compose exec -T db psql -U grosterhit -d grosterhit -c "\dt" | head
```

## 4. Миграции
Дамп содержит схему **в том виде, в каком она была на OLD**. Всё, что появилось в
`db/migrations/` позже последнего применения на OLD, надо накатить на NEW. Миграции
идемпотентны, повтор безопасен — проще применить все по порядку:
```bash
for f in db/migrations/*.sql; do
  echo "== $f"; docker compose exec -T db psql -U grosterhit -d grosterhit < "$f"
done
```

## 5. Файлы вне БД
Картинки, загруженные в конструкторе писем, в git не лежат. На OLD они внутри контейнера
`api` (volume для них появился только сейчас), на NEW — в volume `grosterhit_uploads`.
```bash
# на OLD: выгрузить каталог из контейнера
cd /opt/grosterhit && docker compose cp api:/app/app/static/uploads ./uploads-old
scp -r uploads-old NEW:/tmp/uploads-old

# на NEW: залить в volume (api уже создал его при первом старте)
docker compose cp /tmp/uploads-old/. api:/app/app/static/uploads/
docker compose exec -u root api chown -R 10001 /app/app/static/uploads
```
Очередь и статусы отправок mailer'а — sqlite в volume `grosterhit_mailer_data`. Если на OLD
воркеры остановлены и очередь пуста (обычный случай), переносить не нужно; иначе:
```bash
ssh OLD 'docker run --rm -v grosterhit_mailer_data:/v -w /v alpine tar cf - .' | \
  docker run --rm -i -v grosterhit_mailer_data:/v -w /v alpine tar xf -
```

## 6. Старт всего стека на NEW
```bash
docker compose --profile standalone up -d --build
docker compose ps            # db healthy, api healthy, workers/mailer/nginx up
docker compose logs -f workers | head -30
```

## 7. Переключить витрину
На стороне магазина поменять хост в теге и сниппете корзины на `https://api.example.com`
(и добавить `?ver=` к `src`, чтобы сбить кэш). Пока это не сделано, `cart-ping` продолжает
идти на OLD — новый сервер будет пустым по событиям корзины.

## 8. Выключить OLD
Только после того, как чек-лист ниже зелёный и в логах NEW видно живой трафик витрины:
```bash
# на OLD
docker compose down          # volume с данными остаётся — откат возможен
```

---

## 9. Чек-лист приёмки
```bash
curl -s https://api.example.com/track.js | head -1        # тег витрины
curl -s https://api.example.com/trigger.js | head -1      # сниппет корзины
curl -s https://api.example.com/wheel.js | head -1        # виджет колеса
curl -s -o /dev/null -w "%{http_code}\n" https://api.example.com/health   # 404 снаружи — так задумано
docker compose exec api python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/health').read())"
```
Админка наружу не публикуется — только через SSH-туннель:
```bash
ssh -L 8000:127.0.0.1:8000 NEW      # затем http://localhost:8000/admin
```
В админке проверить: «1С подключена (pull)», индикатор загрузки тега, счётчики подписчиков
и каталога — цифры должны совпасть с OLD.

## 10. После переезда
```bash
# ежедневный бэкап БД (крон на NEW)
echo '0 3 * * * cd /opt/grosterhit && docker compose exec -T db pg_dump -U grosterhit grosterhit | gzip > /var/backups/gh-$(date +\%F).sql.gz' | crontab -
```
- Проверить, что письма уходят (или лежат в dev-логе, если SMTP в `.env` пуст).
- Обновить в памяти проекта топологию: новый хост, новый домен, порт api.

---

## Снимок текущего сервера — 06.09.2026

Собран read-only инвентаризацией (`skyputh@77.93.125.36`). Цифры отсюда — основание для
оценок ниже, перепроверять перед самим переездом.

**Хост.** Ubuntu 24.04, 4 ядра / 15 ГБ RAM, диск 98 ГБ занят на **89 % (свободно 12 ГБ)**,
TZ Europe/Moscow. Внешний IP `77.93.125.36`, в локальной сети `192.168.1.203` — то есть это
машина за пробросом портов, а не арендованный VPS. Docker 29.5.2, compose v5.1.4.
`ufw` не включён; наружу слушают 80/81/443/8000–8090/5432/5433/6543/4530 и т. д.

**Сосед по хосту.** На сервере крутится **ещё ~15 проектов** (supabase, casetop, timelog,
n8n, nocobase, wordpress, nextcloud, portainer, NPM, max-comment-bot и др.), 3 из 4 ядер
заняты постоянно (load ~1,3). Крупнейший едок диска — `/home/skyputh/max-comment-bot` (35 ГБ).
Для GrosterHit это значит: переезд не «переносит сервер», а **вынимает один стек из общего
хозяйства**. Ничего чужого трогать не нужно.

**Стек GrosterHit** (`/opt/grosterhit`, 7 дней аптайма):

| Контейнер | Состояние |
|---|---|
| `grosterhit-db-1` (postgres:16-alpine) | healthy |
| `grosterhit-api-1` | healthy, `127.0.0.1:8001` |
| `grosterhit-workers-1` | up |
| `grosterhit-mailer-1` | up |
| `grosterhit-edge-1` | up, за NPM (статический IP `192.168.16.240`) |
| `grosterhit-nginx-1` | **Restarting в цикле** — см. «Что чинить» |

**Данные — 19 МБ, дамп ≈ 1,0 МБ.** Это ключевой факт: переезд данных занимает секунды,
окно переключения упирается не в объём, а в пересборку образов и правку сниппета витрины.

| Таблица | Строк | | Таблица | Строк |
|---|---:|---|---|---:|
| products | 1943 | | email_templates | 18 |
| top5_by_category | 600 | | script_hits | 12 |
| categories | 249 | | service_config | 3 |
| subscribers | **2** | | email_log | 2 |
| cart_sessions | 1 | | orders / send_queue | 0 |

Каталог живой (последнее обновление `products` — 10.08.2026), а вот подписчиков и заказов
фактически нет: система в пилоте, история рассылок не накоплена. Потерять при переезде
нечего, но и «прогретой» базы, на которую можно опереться при проверке, тоже нет.

> Осторожно со статистикой планировщика: `pg_stat_user_tables.n_live_tup` на этом сервере
> показывает нули почти по всем таблицам (autovacuum/analyze не отрабатывал после заливки
> каталога). Реальные числа получены `count(*)` — при приёмке на новом хосте сверять так же.

**Схема отстаёт от репозитория.** На сервере есть только миграции `001–003`, применены
`001` (колонки колеса в `subscribers`), `002` (`products.tags`), `003` (`script_hits`),
`004` (`products.image_url`). **Нет** `lead_engagement` (006) и `app_settings.items_per_email`
(007); файлов `004`, `005`, `008` на сервере нет вовсе. Код на сервере — коммит `42d1f47`,
**на 4 коммита позади `origin/main`**, образ api собран 11.08.2026. Плюс в рабочей копии
разработки лежат 20 изменённых и 4 новых файла, ещё не закоммиченных.
→ **Переезд и обновление кода — две разные задачи.** Сначала довезти код до сервера
(любого), потом переезжать; иначе на новом хосте окажется августовская версия.

**Внешние связи с этого хоста:**

| Ресурс | Результат |
|---|---|
| `hub.docker.com` | 200 |
| `pypi.org` | **000 (недоступен)** → в `.env` прописан `PIP_INDEX_URL` на зеркало |
| `1c.groster.me` | **000 (недоступен)** |

`ONEC_BASE_URL` и `ONEC_TOKEN` в проде **пусты** — pull из 1С выключен. Вместо боевой 1С
работает systemd-сервис `groster-mock.service` («temporary 1C integration stand», node,
`/opt/groster/mock-server`, состояние в `/var/lib/groster-mock`). Он **не в Docker** и в
compose не описан — при переезде о нём легко забыть.

`SMTP_HOST` пуст → письма идут в dev-лог, реальной отправки нет (совпадает с ожиданиями).
Очередь mailer'а — `outbox.db` ≈ 36 КБ + WAL 260 КБ.

**Публично (проверено с внешнего адреса через `https://groster.skypath.fun`):**
`/track.js` `/trigger.js` `/wheel.js` → 200; `/health` `/kpi` `/feeds/*` → 404 (как задумано);
`/admin` → 401, `/login` → **200**. Форма входа в админку доступна из интернета — так
сделано намеренно (whitelist в `nginx.npm.conf` включает `/login` и `/admin`), но в логах
edge видно, как её обходят краулеры. На новом хосте это повод либо закрыть `/login`
по IP, либо оставить осознанно.

**Бэкапов GrosterHit нет.** Крон в 03:00 (`/home/skyputh/backup.sh`) снимает дамп **только**
`supabase-db` (20 МБ/сутки, 7 дней, выгрузка в Google Drive через rclone). База `grosterhit`
не бэкапится ни разу — то есть сегодня единственная копия каталога живёт в одном volume.
Это отдельный аргумент за переезд и за шаг 10.

**Смежное наблюдение (не GrosterHit).** Ежечасный `/home/skyputh/sync-from-cloud.sh` тянет
дамп с облачного хоста `147.45.146.171:4530` и хранит **пароль к БД открытым текстом в
скрипте**. К переезду отношения не имеет, но если этот облачный хост и есть цель переезда —
пароль стоит ротировать и вынести в файл с правами `600`.

### Что чинить (не блокирует переезд, но поедет с собой)

1. **`grosterhit-nginx-1` в цикле рестарта** уже неделю: `host not found in upstream
   "api:8000"` — это контейнер профиля `standalone`, который за NPM подниматься не должен
   (80/443 держит NPM). Лечится `docker compose stop grosterhit-nginx-1` на старом хосте;
   на новом — не поднимать профиль `standalone`, если ставится NPM, и наоборот.
2. **`DATABASE_URL` в `.env` = `postgresql://localhost/grosterhit_dev`** — прод работает
   только потому, что compose перебивает эту переменную. Любой скрипт, запущенный вне
   compose (`python scripts/seed.py`, ручная миграция), пойдёт мимо боевой БД. При
   копировании `.env` на новый хост — привести к реальному значению.
3. **`groster-mock.service`** перенести отдельно (unit + `/opt/groster/mock-server` +
   `/etc/groster-mock.env` + состояние `/var/lib/groster-mock/state.json`) — либо признать
   его ненужным и на новом хосте подключить боевую 1С.

### Что это меняет в плане ниже

- **Шаг 0.1 (allowlist 1С)** сейчас неактуален: pull выключен, стоит мок. Решение
  «включаем боевую 1С на новом хосте или везём мок» принять до переезда.
- **Шаг 5 (uploads)**: каталога `/app/app/static/uploads` в контейнере api **нет** —
  картинки через конструктор не загружались. Переносить нечего, шаг пропускается.
- **Окно переключения**: дамп 1 МБ, восстановление — секунды. Реальное время окна съедает
  сборка образов (`pip install` через зеркало) и правка тега на витрине.
- **Диск на новом хосте**: стеку нужно ~1,5 ГБ (образы 3 × ~185 МБ + БД 19 МБ + запас).
  Любой VPS от 20 ГБ подойдёт с большим запасом.

---

## Снимок целевого сервера — 06.09.2026

`vl4en-95@93.77.165.78`, hostname уже `grosterhit`, аптайм 21 минута — машина выдана
сегодня и пуста. Ubuntu 24.04.4, Xeon Gold 6338.

| | Старый (skyputh) | Новый (93.77.165.78) |
|---|---|---|
| Роль | общий хост, ~15 проектов | **выделен под GrosterHit** |
| CPU / RAM | 4 / 15 ГБ (load ~1,3) | **2 / 1,9 ГБ**, swap нет |
| Диск | 98 ГБ, занято 89 % | 29 ГБ, занято 11 % (26 ГБ свободно) |
| Занятые порты | 80, 81, 443, 8000–8090, 5432… | **только 22** |
| Docker | 29.5.2 / compose v5.1.4 | 29.8.0 / compose v5.5.1 (уже стоит) |
| Реверс-прокси | Nginx Proxy Manager | нет |
| `pypi.org` | **000 (блок)** → нужно зеркало | **200** |
| `1c.groster.me` | 000 | 000 (резолвится в 45.93.201.96, но 443 молчит) |
| `groster.me` / `static.groster.me` | — | 200 / 200 |
| TZ | Europe/Moscow | **UTC** |

Установлены `git`, `certbot`, `rsync`; `psql` на хосте нет (не нужен — через `docker exec`).
`sudo` без пароля есть. Пользователь `vl4en-95` **не в группе `docker`** — либо `sudo docker`,
либо `sudo usermod -aG docker vl4en-95` + перелогин. Старый сервер доступен отсюда по
22/tcp — дамп можно тянуть напрямую, без промежуточной машины.

### Следствия для плана

1. **NPM на новом хосте нет, 80/443 свободны** → едем по варианту «standalone»:
   `docker compose --profile standalone up -d` + хостовый certbot. Файл
   `docker-compose.edge.yml` и статический IP `192.168.16.240` на новом хосте не нужны —
   это костыль под NPM. Публичный whitelist берётся из `deploy/nginx.docker.conf`.
2. **`PIP_INDEX_URL` убрать** — pypi доступен напрямую, зеркало было обходом сетевого
   ограничения старого хоста. Оставите — сборка пойдёт через Aliyun без нужды.
3. **1,9 ГБ RAM без swap — единственное узкое место.** Стек (postgres + api + workers +
   mailer + nginx) в покое укладывается примерно в 600–800 МБ, но `docker compose build`
   с `pip install` даёт пик. Перед первой сборкой добавить своп на 2 ГБ:
   ```bash
   sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile \
     && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```
4. **Часовой пояс UTC против Europe/Moscow на старом.** Воркеры считают окна отправки и
   задержки в БД (`POSTSALE_DELAY_DAYS`, `CART_COOLDOWN_HOURS`) — в схеме везде `TIMESTAMPTZ`,
   так что арифметика не поедет, но логи и админка будут в UTC. Либо принять, либо
   `sudo timedatectl set-timezone Europe/Moscow` — чтобы время в логах совпадало с тем,
   к чему привыкли.
5. **1С недоступна и с нового хоста** — имя резолвится, но 443 не отвечает. То есть переезд
   ничего не чинит: боевую 1С включать не с чего, `groster-mock.service` придётся везти
   с собой (или отказаться от него осознанно).
6. **DNS**: `groster.skypath.fun` сейчас указывает на `77.93.125.36`. Под новый поддомен
   нужна отдельная A-запись на `93.77.165.78` в Timeweb.
