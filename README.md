# LeadHit-замена — триггерные email-сервисы для groster.me

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
| PUT | `/feeds/{categories,products,top5,subscribers,orders}` | приём фидов |
| POST | `/cart-ping` | heartbeat корзины (session-ping) |
| POST | `/esp/webhook` | статусы письма от ESP |
| GET | `/kpi` | 6 метрик по сервису + бенчмарк + флаг просадки |
| GET | `/admin` | админ-панель (ТЗ 8.1): настройки, логи, фиды, ручной запуск |
| GET/PUT | `/admin/config[/{service}]` | вкл/выкл + интервалы/cooldown (без перезапуска) |
| GET | `/admin/{logs,feeds-status}` | просмотр логов, мониторинг актуальности фидов |
| POST | `/admin/run/{service}` | ручной запуск воркера |

## Источник данных: 1С (pull) или push-фиды
- **Прод**: каталог, корзина и статус заказа тянутся из 1С по HTTP — контракт в
  [db/1c_contract.md](db/1c_contract.md), клиент в `app/onec.py`. Включается заданием
  `ONEC_BASE_URL` (+ `ONEC_TOKEN`) в `.env`. Синхронизация каталога — в `worker_loop`
  раз/сутки; ручной запуск — кнопкой в разделе «Фиды» или `POST /admin/sync-catalog`.
- **Дев/тест**: `ONEC_BASE_URL` пуст → 1С не используется, данные грузятся push-фидами
  `PUT /feeds/*` (см. `scripts/seed.py`). Логика корзины/заказов падает на локальную БД.

## Заглушки (по плану, не техдолг)
- **LogMailer** — печатает письмо в лог. Реальный ESP подключается реализацией
  `Mailer.send()` в `app/mailer.py` (см. `get_mailer`), воркеры не меняются.
- **KPI — JSON**; полноценная админ-панель (ТЗ 8.1) — отдельная задача.
- **Тайминги ухода/grace** правятся через `.env` (`CART_DEPART_TIMEOUT_SEC`, `CART_GRACE_SEC`).

## Перед проливом в прод
Закрыть 8 открытых вопросов к заказчику (раздел «Открытые вопросы» роадмапа):
форматы фидов, идентификация анонима в корзине, окно атрибуции, согласие 152-ФЗ и др.
Плюс: подключить ESP, настроить DKIM/SPF/DMARC + прогрев домена, поднять воркеры на cron.
