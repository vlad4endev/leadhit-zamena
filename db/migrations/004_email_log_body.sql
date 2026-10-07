-- Снимок письма, ушедшего клиенту: тема + HTML как отправили.
-- Нужен, чтобы в журнале открыть письмо целиком (шаблон с товарами),
-- а не только метаданные. Идемпотентно.
ALTER TABLE email_log ADD COLUMN IF NOT EXISTS subject TEXT;
ALTER TABLE email_log ADD COLUMN IF NOT EXISTS html TEXT;
