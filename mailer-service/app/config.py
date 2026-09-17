"""Настройки mailer-service из окружения."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Транспорт: sendmail | smtp | '' (авто: sendmail если бинарь есть и smtp пуст).
    mail_transport: str = ""
    sendmail_path: str = "/usr/sbin/sendmail"

    # SMTP-relay ESP. Пусто host + не sendmail → dev-режим (лог).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    mail_from: str = "noreply@mail.groster.me"
    mail_from_name: str = "groster.me"

    # Очередь и доставка.
    db_path: str = "outbox.db"
    rate_per_min: int = 120        # антифлуд / прогрев домена
    max_attempts: int = 5          # ретраи при сбое
    retry_base_sec: int = 30       # экспоненциальный бэкофф

    # Обратный вызов в основное приложение при событиях доставки.
    callback_url: str = ""         # напр. http://api:8000/esp/webhook
    callback_token: str = ""

    # Защита нашего API.
    api_token: str = ""


settings = Settings()
