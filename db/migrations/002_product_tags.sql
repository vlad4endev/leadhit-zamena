-- Членство товара в фидах (Новинка/Топ/Сопутствующий) для фильтра в каталоге.
-- Идемпотентно: можно применять на прод повторно.
ALTER TABLE products ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS products_tags_idx ON products USING GIN (tags);
