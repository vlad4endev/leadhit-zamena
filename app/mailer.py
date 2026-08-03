"""Отправка писем. Абстракция + dev-лог + SMTP-relay + внешний mailer-service.

Приоритет: mailer-service (микросервис) → SMTP-relay → dev-лог. Воркеры сценариев
зависят только от Mailer.send(). meta пробрасывается в mailer-service для связи событий.
"""
from __future__ import annotations

import asyncio
import json
import smtplib
import urllib.request
from email.message import EmailMessage
from typing import Optional, Protocol

from app.config import settings


class Mailer(Protocol):
    async def send(self, to: str, subject: str, html: str,
                   from_email: str = "", from_name: str = "", meta: Optional[dict] = None) -> bool:
        ...


class LogMailer:
    """Dev-отправитель: печатает письмо в лог, всегда 'успех'. Для локали и тестов."""

    async def send(self, to: str, subject: str, html: str,
                   from_email: str = "", from_name: str = "", meta: Optional[dict] = None) -> bool:
        print(f"[MAIL] from={from_email or settings.mail_from} to={to} subject={subject!r} html_len={len(html)}")
        return True


class HttpMailer:
    """Отправка через mailer-service: POST /v1/send. Микросервис держит очередь и ESP."""

    async def send(self, to: str, subject: str, html: str,
                   from_email: str = "", from_name: str = "", meta: Optional[dict] = None) -> bool:
        body = {"to": to, "subject": subject, "html": html,
                "from_email": from_email, "from_name": from_name, "meta": meta or {}}
        try:
            await asyncio.to_thread(self._post, body)
            return True
        except Exception as e:  # noqa: BLE001 — недоступность сервиса логируем, письмо не теряем
            print(f"[MAILER-SVC-ERROR] to={to} {type(e).__name__}: {e}")
            return False

    def _post(self, body: dict) -> None:
        headers = {"Content-Type": "application/json"}
        if settings.mailer_service_token:
            headers["Authorization"] = f"Bearer {settings.mailer_service_token}"
        req = urllib.request.Request(
            settings.mailer_service_url.rstrip("/") + "/v1/send",
            data=json.dumps(body).encode(), method="POST", headers=headers)
        urllib.request.urlopen(req, timeout=15).read()


class SmtpMailer:
    """Отправка через SMTP-relay ESP. Блокирующий smtplib вынесен в поток (asyncio.to_thread)."""

    async def send(self, to: str, subject: str, html: str,
                   from_email: str = "", from_name: str = "", meta: Optional[dict] = None) -> bool:
        try:
            await asyncio.to_thread(self._send_sync, to, subject, html, from_email, from_name)
            return True
        except Exception as e:  # noqa: BLE001 — bounce/отказ логируем, письмо не теряем
            print(f"[MAIL-ERROR] to={to} {type(e).__name__}: {e}")
            return False

    def _send_sync(self, to: str, subject: str, html: str, from_email: str, from_name: str) -> None:
        msg = EmailMessage()
        msg["From"] = f"{from_name or settings.mail_from_name} <{from_email or settings.mail_from}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content("Для просмотра письма включите HTML.")
        msg.add_alternative(html, subtype="html")

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            if settings.smtp_starttls:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)


def get_mailer() -> Mailer:
    # Приоритет: mailer-service → SMTP-relay → dev-лог.
    if settings.mailer_service_url:
        return HttpMailer()
    if settings.smtp_host:
        return SmtpMailer()
    return LogMailer()
