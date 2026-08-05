"""Отправка через SMTP-relay провайдера + обратный вызов событий в основное приложение."""
from __future__ import annotations

import asyncio
import json
import smtplib
import urllib.request
from email.message import EmailMessage
from email.utils import make_msgid

from app.config import settings
from app import store


def send_sync(to: str, subject: str, html: str, from_email: str, from_name: str) -> str:
    """Отправляет письмо, возвращает Message-ID. dev-режим (нет smtp_host) — только лог."""
    cfg = store.config_sync()
    message_id = make_msgid(domain=(cfg["mail_from"].split("@")[-1] or "mail.local"))
    if not cfg["smtp_host"]:
        print(f"[DEV-MAIL] to={to} subject={subject!r} mid={message_id} html_len={len(html)}")
        return message_id

    msg = EmailMessage()
    msg["Message-ID"] = message_id
    # From всегда = авторизованный ящик (mail_from): SMTP-релеи (Яндекс, Mail.ru и др.) отклоняют
    # чужой From — «550 not local sender». Адрес отправителя сценария кладём в Reply-To, чтобы
    # ответы клиентов уходили на него, а не на технический ящик.
    msg["From"] = f"{from_name or cfg['mail_from_name']} <{cfg['mail_from']}>"
    if from_email and from_email != cfg["mail_from"]:
        msg["Reply-To"] = from_email
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("Для просмотра письма включите HTML.")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as s:
        if cfg["smtp_starttls"]:
            s.starttls()
        if cfg["smtp_user"]:
            s.login(cfg["smtp_user"], cfg["smtp_password"])
        s.send_message(msg)
    return message_id


def _callback_sync(payload: dict) -> None:
    if not settings.callback_url:
        return
    headers = {"Content-Type": "application/json"}
    if settings.callback_token:
        headers["Authorization"] = f"Bearer {settings.callback_token}"
    req = urllib.request.Request(
        settings.callback_url, data=json.dumps(payload).encode(), method="POST", headers=headers)
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # noqa: BLE001 — доставка события best-effort
        print(f"[callback-fail] {type(e).__name__}: {e}")


async def send(to, subject, html, from_email, from_name) -> str:
    return await asyncio.to_thread(send_sync, to, subject, html, from_email, from_name)


async def callback(meta: dict, event: str) -> None:
    """Пробрасывает событие доставки в основное приложение (meta + event)."""
    await asyncio.to_thread(_callback_sync, {**meta, "event": event})
