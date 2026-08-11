# GrosterHit — триггерные email-сервисы для groster.me

Собственная замена внешнего LeadHit. Три сценария, дающие 82,8% дохода от триггерных
рассылок: **Best Offer**, **Брошенная корзина**, **Постпродажа**. Без ML и без сбора
поведения на сайте — релевантность через фид «топ-5 по категориям» от заказчика.

Полное ТЗ и план — в [ROADMAP_LeadHit_замена.md](ROADMAP_LeadHit_замена.md).

## Стек
Python + FastAPI + PostgreSQL + asyncpg. Очереди — на PostgreSQL (`SKIP LOCKED`),
отдельный брокер не нужен. Прод — Python 3.11+ (локально работает и на 3.9).

## Структура
```
app/
  main.py        точка входа, роутеры, lifecycle пула
  config.py      настройки + тайминги сценариев (из .env)
  db.py          пул asyncpg
  feeds.py       приём фидов: categories/products/top5/subscribers/orders
  import_xml.py  импорт каталога/топ-5/подписчиков из XML-файла (админка); диспетчер формата
  import_yml.py  импорт каталога из YML (Yandex Market Language) — стандартной выгрузки витрины
  mailer.py      Mailer (абстракция) + LogMailer (dev); реальный ESP — одна реализация
  templates.py   общий email-макет (товарный блок + UTM + отписка)
  postsale.py    Сервис 3 — Постпродажа (delayed-job +7д)
  best_offer.py  Сервис 1 — Best Offer (батч, ротация категорий + дедуп)
  cart.py        Сервис 2 — Брошенная корзина (session-ping, near-real-time)
  analytics.py   вебхуки ESP, атрибуция дохода, KPI vs бенчмарк LeadHit
db/
  schema.sql     схема БД
  seed.json      тестовые данные
scripts/
  seed.py        загрузчик seed через API
  run_worker.py  прогон воркеров (в проде — cron/цикл)
```

## Быстрый старт
```bash
make venv          # venv + зависимости + .env
make db            # создать БД и применить схему
make run           # поднять API (в отдельном терминале)
make seed          # загрузить тестовые данные
make test          # self-check логики всех сервисов
```

## Воркеры
```bash
make worker-postsale     # отправка Постпродажи (наступил 7-й день)
make worker-best-offer   # батч Best Offer (условие 30/20 дней)
make worker-cart         # тик near-real-time корзины
make attribution         # привязка заказов к письмам (окно 72ч)
```
В проде: Best Offer — cron раз/сутки; Корзина — непрерывный цикл; Постпродажа —
delayed-job; атрибуция — по расписанию.

## API
| Метод | Путь | Назначение |
|---|---|---|
| GET | `/health` | статус + пинг БД |
| GET | `/trigger.js` | триггер-сниппет для встраивания на groster.me (session-ping корзины) |
| GET | `/demo` | dev/reference: эталон разводки `cart()`/`identify()` (см. docs/site_integration.md) |
| PUT | `/feeds/{categories,products,top5,subscribers,orders}` | приём фидов |
| POST | `/cart-ping` | heartbeat корзины (session-ping) |
| POST | `/order` | код заказа с thank-you page: снимает сессии с рассылки, даёт выручку атрибуции |
| POST | `/esp/webhook` | статусы письма от ESP |
| GET | `/unsubscribe` | отписка по ссылке из футера (подтверждение → `confirm=1`) |
| GET | `/kpi` | 6 метрик по сервису + бенчмарк + флаг просадки |
| GET | `/admin` | админ-панель (ТЗ 8.1): настройки, логи, фиды, ручной запуск, раздел «Интеграция» |
| GET/PUT | `/admin/config[/{service}]` | вкл/выкл + интервалы/cooldown (без перезапуска) |
| GET | `/admin/{logs,feeds-status}` | просмотр логов, мониторинг актуальности фидов |
| GET | `/admin/integration` | статус интеграции: режим отправки, 1С, стата связи по шагам за 24ч |
| GET | `/admin/selftest` | самопроверка подключения: видны ли скрипты снаружи, CORS, каталог, `product_id` |
| POST | `/admin/run/{service}` | ручной запуск воркера |
| POST | `/admin/import-xml` | импорт каталога/топ-5/подписчиков из файла — свой XML или YML (тело = файл) |

## Триггер на сайте (брошенная корзина)
Клиентская часть сервиса 2 — «намеренно тупой» сниппет (`app/static/trigger.js`,
ROADMAP 3.1): один таймер + один POST `/cart-ping`, без сбора поведения (152-ФЗ).
Встраивание на groster.me:
```html
<script src="https://groster.skypath.fun/trigger.js" data-interval="45"></script>
<script>
  // на каждое изменение корзины:
  groster.cart([{ product_id: 'A1', qty: 1 }]);   // обязателен только product_id
  // при логине или вводе email на оформлении (идентификация анонима, ROADMAP 3.4).
  // consent: true — только если пользователь поставил галочку согласия на письма (152-ФЗ).
  // Backend заводит подписчика с consent_at ТОЛЬКО при consent:true; без него письма не будет.
  groster.identify({ user_id: '12345', email: 'user@example.com', consent: true });
</script>
```
Сниппет сам держит `session_id` (cookie `gr_sid`, 1 год), пингует только при активной
вкладке и непустой корзине, помнит последнюю личность (cookie-приоритет 3.4). Адрес сервиса
берётся из `src` тега (`data-endpoint` — переопределение). Домены витрины для CORS — в админке
(«Интеграция» → «Адреса и домены») поверх `CORS_ORIGINS` из `.env`, применяются без рестарта.
Self-check логики: `node scripts/test_trigger.js`.

Проще — один тег `track.js` (`app/static/track.js`): грузит сниппет сам, ловит email из форм,
дренит `grDataLayer` и **читает готовые ecommerce-события GA4/GTM** из `window.dataLayer` —
на витрине с GA4-разметкой код писать не нужно вообще. Диагностика интеграции — `?grdebug=1`
на любой странице: логи вызовов + незнакомые каталогу `product_id` в ответе `/cart-ping`.

Полный гайд разводки для витринной команды (события, edge-cases, примеры под
Битрикс/сервер-рендер/SPA) — [docs/site_integration.md](docs/site_integration.md).
Рабочий эталон с фейковой корзиной и живым логом — `GET /demo`.

Отдавать интегратору удобнее из админки: `/admin` → **«Интеграция»** — тот же код по вкладкам
(тег, корзина, логин, email+согласие, заказ, колесо, ручной `trigger.js`, приёмка) с
подставленным `PUBLIC_BASE_URL`, плюс статистика связи: по каким шагам данные реально доходят
за 24ч и сколько сессий шлёт `product_id` не из каталога. В шапке админки — индикатор связи
по двум сигналам: загрузки тега (`script_hits`, агрегат по дням без ПД) и свежесть `cart-ping`;
зелёный только при живых событиях, красный — когда молчат оба. Вкладка «Проверка» — самопроверка
в один клик (`/admin/selftest`): сервис сам ходит по своему публичному адресу и показывает,
видит ли витрина каждый скрипт (ловит непрописанный на edge путь, как было с `/track.js`).

## Источник данных
- **Заказы**: три источника, все идемпотентны по `order_id`. Живой HTTP из 1С
  (`/orders/exists`, гейт «уже купил») — контракт в [db/1c_contract.md](db/1c_contract.md),
  клиент в `app/onec.py`, включается `ONEC_BASE_URL`. Push сервер-сервер `PUT /feeds/orders`.
  И `POST /order` со страницы «спасибо» (`groster.order()`): единственный путь, не требующий
  ничего от 1С. Статусом заказа владеет 1С — существующий заказ витрина не переписывает.
- **Каталог, топ-5, подписчики**: импорт **файлом** (загрузка в админке `/admin` →
  «Импорт из файла») — заменяет живой `/catalog` из 1С. Принимаются свой XML
  ([db/import_contract.md](db/import_contract.md), [пример](db/import_sample.xml),
  парсер `app/import_xml.py`), стандартный **YML** Яндекс.Маркета (`app/import_yml.py`,
  формат определяется по корню файла) и JSON-выгрузка витрины (`app/import_json.py`).
  Все три сводятся к одной структуре и общему `import_xml.import_all` — upsert'ы из `feeds.py`.
- **Корзина**: из 1С **не запрашивается** — истина на момент отправки берётся из пинга сниппета
  (`cart_sessions`, решение п.2).
- **Дев/тест**: push-фиды `PUT /feeds/*` (см. `scripts/seed.py`).

## Деплой
- **Docker (рекомендуется)**: весь стек (БД, API, воркеры, mailer, nginx+TLS) — в
  `docker compose`. Инструкция: [deploy/DOCKER.md](deploy/DOCKER.md). Файлы: `Dockerfile`,
  `docker-compose.yml`, `deploy/nginx.docker.conf`.
- **systemd на хосте** (альтернатива): [deploy/README.md](deploy/README.md).

## Заглушки (по плану, не техдолг)
- **LogMailer** — печатает письмо в лог. Реальный ESP подключается реализацией
  `Mailer.send()` в `app/mailer.py` (см. `get_mailer`), воркеры не меняются.
- **KPI — JSON**; полноценная админ-панель (ТЗ 8.1) — отдельная задача.
- **Тайминги ухода/grace** правятся через `.env` (`CART_DEPART_TIMEOUT_SEC`, `CART_GRACE_SEC`).

## Перед проливом в прод
Закрыть 8 открытых вопросов к заказчику (раздел «Открытые вопросы» роадмапа):
форматы фидов, идентификация анонима в корзине, окно атрибуции, согласие 152-ФЗ и др.
Плюс: подключить ESP, настроить DKIM/SPF/DMARC + прогрев домена, поднять воркеры на cron.
