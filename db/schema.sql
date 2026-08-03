-- LeadHit-замена: схема БД (Этап 0)
-- Общая основа для трёх сервисов: Best Offer, Брошенная корзина, Постпродажа.
-- Один email_log закрывает дедуп, антидубль и всю аналитику раздела 6 ТЗ.

CREATE TYPE service_kind AS ENUM ('best_offer', 'cart', 'postsale');

CREATE TYPE email_status AS ENUM (
    'queued', 'sent', 'delivered', 'opened', 'clicked', 'bounced', 'unsubscribed', 'failed'
);

CREATE TYPE queue_state AS ENUM (
    'scheduled', 'queued', 'sent', 'cancelled', 'failed'
);

CREATE TYPE session_state AS ENUM ('active', 'departed', 'converted', 'sent');

-- Справочник категорий в ФИКСИРОВАННОМ порядке (старт ротации для юзеров без покупок).
CREATE TABLE categories (
    category_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    sort_order   INT  NOT NULL UNIQUE
);

-- Зеркало каталога магазина (product feed).
CREATE TABLE products (
    product_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    price        NUMERIC(12,2) NOT NULL,
    image_url    TEXT,
    category_id  TEXT NOT NULL REFERENCES categories(category_id),
    product_url  TEXT NOT NULL,
    in_stock     BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX products_category_idx ON products(category_id) WHERE in_stock;

-- Фид «топ-5 по категориям» от заказчика (источник релевантности вместо ML).
CREATE TABLE top5_by_category (
    category_id  TEXT NOT NULL REFERENCES categories(category_id),
    position     INT  NOT NULL CHECK (position BETWEEN 1 AND 5),
    product_id   TEXT NOT NULL REFERENCES products(product_id),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (category_id, position)
);

-- Профиль подписчика: таймеры, указатель ротации, статус согласия/отписки.
CREATE TABLE subscribers (
    user_id                    TEXT PRIMARY KEY,
    email                      TEXT,
    is_unsubscribed            BOOLEAN NOT NULL DEFAULT FALSE,
    consent_at                 TIMESTAMPTZ,                 -- 152-ФЗ: фиксация согласия
    last_purchase_at           TIMESTAMPTZ,
    last_purchase_category_id  TEXT REFERENCES categories(category_id),
    rotation_pointer_category_id TEXT REFERENCES categories(category_id),
    last_sent_best_offer_at    TIMESTAMPTZ,
    last_sent_cart_at          TIMESTAMPTZ,
    last_sent_postsale_at      TIMESTAMPTZ,
    last_any_trigger_at        TIMESTAMPTZ                  -- антидубль (1 триггер/день)
);
CREATE INDEX subscribers_email_idx ON subscribers(email) WHERE email IS NOT NULL;

-- Зеркало/реплика заказов магазина (нового сбора нет).
CREATE TABLE orders (
    order_id     TEXT PRIMARY KEY,
    user_id      TEXT REFERENCES subscribers(user_id),
    email        TEXT,
    order_date   TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL,                             -- new|paid|cancelled|returned|...
    items        JSONB NOT NULL                             -- [{product_id, category_id, price, qty}]
);
CREATE INDEX orders_user_idx ON orders(user_id);
CREATE INDEX orders_date_idx ON orders(order_date);

-- Сердце дедупликации и аналитики: одна строка на письмо.
CREATE TABLE email_log (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES subscribers(user_id),
    service             service_kind NOT NULL,
    category_id         TEXT REFERENCES categories(category_id),
    product_ids         TEXT[] NOT NULL DEFAULT '{}',       -- что отправили (дедуп Best Offer)
    order_id            TEXT REFERENCES orders(order_id),   -- Постпродажа: 1 письмо на заказ
    status              email_status NOT NULL DEFAULT 'queued',
    sent_at             TIMESTAMPTZ,
    delivered_at        TIMESTAMPTZ,
    opened_at           TIMESTAMPTZ,
    clicked_at          TIMESTAMPTZ,
    attributed_order_id TEXT REFERENCES orders(order_id),   -- атрибуция дохода (постфактум)
    revenue             NUMERIC(12,2),
    template_id         BIGINT,   -- какой шаблон использован (FK добавляется после email_templates)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX email_log_user_service_idx ON email_log(user_id, service, created_at DESC);
-- Постпродажа: не дублировать письмо на один заказ.
CREATE UNIQUE INDEX email_log_postsale_order_uidx
    ON email_log(order_id) WHERE service = 'postsale' AND order_id IS NOT NULL;

-- Очередь отправки: cron (Best Offer), delayed-job (Постпродажа), NRT (Корзина).
-- Разбирается воркерами через SELECT ... FOR UPDATE SKIP LOCKED.
CREATE TABLE send_queue (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES subscribers(user_id),
    service     service_kind NOT NULL,
    order_id    TEXT REFERENCES orders(order_id),           -- для Постпродажи
    payload     JSONB NOT NULL DEFAULT '{}',
    run_after   TIMESTAMPTZ NOT NULL DEFAULT now(),
    state       queue_state NOT NULL DEFAULT 'scheduled',
    attempts    INT NOT NULL DEFAULT 0,
    locked_at   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Индекс под воркер: что готово к обработке.
CREATE INDEX send_queue_pickup_idx
    ON send_queue(service, run_after) WHERE state IN ('scheduled', 'queued');
-- Постпродажа: одна задача на заказ (идемпотентная постановка при повторном приёме заказа).
CREATE UNIQUE INDEX send_queue_postsale_order_uidx
    ON send_queue(order_id) WHERE service = 'postsale' AND order_id IS NOT NULL;

-- Глобальные настройки приложения (админка). Правятся без перезапуска (кроме интервалов воркеров).
CREATE TABLE app_config (
    key   TEXT PRIMARY KEY,
    value JSONB NOT NULL
);

-- Настройки сервисов (админка, ТЗ 8.1): вкл/выкл + бизнес-параметры (интервалы, cooldown).
-- Читаются воркерами на каждом тике → правки из админки действуют без перезапуска.
CREATE TABLE service_config (
    service    service_kind PRIMARY KEY,
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    params     JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()   -- «Запущена» (когда сценарий заведён)
);
INSERT INTO service_config(service, params) VALUES
    ('best_offer', '{"interval_days":30,"after_purchase_days":20}'),
    ('cart',       '{"cooldown_hours":72,"depart_timeout_sec":180,"grace_sec":90}'),
    ('postsale',   '{"delay_days":7}');

-- Шаблоны писем из конструктора. На сценарий может быть несколько шаблонов;
-- отправляет активный (is_active). Нет активного → сценарий рендерит дефолт.
CREATE TABLE email_templates (
    id         BIGSERIAL PRIMARY KEY,
    service    service_kind,          -- NULL = черновик, не прикреплён к сценарию
    name       TEXT,
    blocks     JSONB NOT NULL,
    is_active  BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Не более одного активного шаблона на сценарий.
CREATE UNIQUE INDEX email_templates_active_ux ON email_templates(service) WHERE is_active;
-- FK от email_log (объявлен выше) на шаблон.
ALTER TABLE email_log ADD CONSTRAINT email_log_template_id_fkey
    FOREIGN KEY (template_id) REFERENCES email_templates(id) ON DELETE SET NULL;

-- Сессии корзины (только для Брошенной корзины, Этап 3).
-- Только heartbeat «жив + корзина непуста», без истории просмотров.
CREATE TABLE cart_sessions (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT REFERENCES subscribers(user_id),
    email        TEXT,
    cart_items   JSONB NOT NULL DEFAULT '[]',                -- [{product_id, category_id, price, qty}]
    cart_hash    TEXT,
    state        session_state NOT NULL DEFAULT 'active',
    last_ping_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    departed_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Индекс под воркер детекта ухода.
CREATE INDEX cart_sessions_active_idx ON cart_sessions(last_ping_at) WHERE state = 'active';
CREATE INDEX cart_sessions_departed_idx ON cart_sessions(departed_at) WHERE state = 'departed';
