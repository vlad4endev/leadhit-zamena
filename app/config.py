"""Настройки из окружения. Параметры сценариев правятся здесь/в админке, не в коде."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://localhost/grosterhit_dev"

    # Внешний mailer-service (микросервис отправки). Задан → письма идут через него.
    mailer_service_url: str = ""
    mailer_service_token: str = ""

    # SMTP-relay ESP. Пусто host → dev-LogMailer.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    mail_from: str = "noreply@mail.groster.me"
    mail_from_name: str = "groster.me"

    # 1С HTTP-сервис (pull). Пустой base_url → 1С не используется (дев работает на push-фидах).
    onec_base_url: str = ""
    onec_token: str = ""
    onec_sync_tick_sec: int = 86400  # синхронизация каталога раз/сутки

    # Интервалы воркеров (сек) для worker_loop.
    cart_tick_sec: int = 30
    postsale_tick_sec: int = 300
    attribution_tick_sec: int = 300
    best_offer_tick_sec: int = 3600  # ежечасно; гейт send_hour держит «раз/сутки в заданный час»

    # Тайминги сценариев (ТЗ разделы 2–4).
    best_offer_interval_days: int = 30
    best_offer_after_purchase_days: int = 20
    postsale_delay_days: int = 7
    cart_cooldown_hours: int = 72
    cart_depart_timeout_sec: int = 180
    cart_grace_sec: int = 90
    attribution_window_hours: int = 72

    # CORS для триггер-сниппета (cart-ping идёт кросс-доменно с groster.me).
    # Пусто → "*" (дев). Прод: "https://groster.me,https://www.groster.me".
    cors_origins: str = ""


settings = Settings()
