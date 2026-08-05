-- Миграция для «Колеса фортуны». Идемпотентна — можно гонять повторно.
-- Прод: schema.sql применяется только к новой БД, существующую обновляем этим файлом.
--   psql "$DATABASE_URL" -f db/migrations/001_wheel.sql
--
-- Без неё после выката кода /wheel-lead и /wheel-prize упадут 500 (нет колонок).

ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS wheel_spun_at    TIMESTAMPTZ; -- 1 прокрут на email
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS wheel_prize_code TEXT;        -- выданный промокод (идемпотентность письма)
