-- Диагностический журнал: этапы авторассылок, skip/ошибки, действия админки.
-- Не путать с email_log (журнал писем) — здесь операционные события для диагностики.
CREATE TABLE IF NOT EXISTS activity_log (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    level       TEXT NOT NULL CHECK (level IN ('debug', 'info', 'warn', 'error')),
    source      TEXT NOT NULL,          -- best_offer|cart|postsale|mailer|worker|admin|onec|import|system
    event       TEXT NOT NULL,          -- batch_start|skip|send_ok|send_failed|queue_cancel|...
    service     service_kind,           -- nullable: сценарий, если применимо
    user_id     TEXT,
    session_id  TEXT,
    order_id    TEXT,
    ref_id      BIGINT,                 -- email_log.id / send_queue.id
    message     TEXT NOT NULL,
    details     JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS activity_log_created_idx ON activity_log(created_at DESC);
CREATE INDEX IF NOT EXISTS activity_log_level_idx ON activity_log(level, created_at DESC);
CREATE INDEX IF NOT EXISTS activity_log_source_idx ON activity_log(source, event, created_at DESC);
CREATE INDEX IF NOT EXISTS activity_log_service_idx ON activity_log(service, created_at DESC)
    WHERE service IS NOT NULL;
