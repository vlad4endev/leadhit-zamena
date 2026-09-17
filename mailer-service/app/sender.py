"""Отправка через sendmail / SMTP-relay + обратный вызов событий в основное приложение."""
from __future__ import annotations

import asyncio
import json
import os
import smtplib
import subprocess
import urllib.request
from email.message import EmailMessage
from email.utils import make_msgid

from app.config import settings
from app import store


def _provider(cfg: dict) -> str:
    """smtp | sendmail | dev — по конфигу."""
    t = (cfg.get("mail_transport") or "").strip().lower()
    path = cfg.get("sendmail_path") or settings.sendmail_path or "/usr/sbin/sendmail"
    if t == "smtp":
        return "smtp" if cfg.get("smtp_host") else "dev"
    if t == "sendmail":
        return "sendmail" if os.access(path, os.X_OK) else "dev"
    if cfg.get("smtp_host"):
        return "smtp"
    if os.access(path, os.X_OK):
        return "sendmail"
    return "dev"


def provider_name(cfg: dict | None = None) -> str:
    return _provider(cfg or store.config_sync())


def _assert_mta_alive() -> None:
    """sendmail часто возвращает 0 даже при остановленном postfix — ловим «mail system is down»."""
    postqueue = "/usr/sbin/postqueue"
    if not os.access(postqueue, os.X_OK):
        postqueue = "postqueue"
    try:
        q = subprocess.run(
            [postqueue, "-p"], capture_output=True, timeout=10, check=False)
    except FileNotFoundError:
        return
    except Exception:  # noqa: BLE001
        return
    err = (q.stderr or q.stdout or b"").decode(errors="replace").lower()
    if q.returncode != 0 and ("mail system is down" in err or "unavailable" in err):
        raise RuntimeError(
            "sendmail принял письмо, но MTA не запущен (postqueue: mail system is down). "
            "Запустите postfix/exim на сервере и повторите тест.")


def send_sync(to: str, subject: str, html: str, from_email: str, from_name: str) -> str:
    """Отправляет письмо, возвращает Message-ID. provider=dev — только лог."""
    cfg = store.config_sync()
    message_id = make_msgid(domain=(cfg["mail_from"].split("@")[-1] or "mail.local"))
    mode = _provider(cfg)

    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["From"] = f"{from_name or cfg['mail_from_name']} <{cfg['mail_from']}>"
    if from_email and from_email != cfg["mail_from"]:
        msg["Reply-To"] = from_email
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("Для просмотра письма включите HTML.")
    msg.add_alternative(html, subtype="html")

    if mode == "dev":
        print(f"[DEV-MAIL] to={to} subject={subject!r} mid={message_id} html_len={len(html)}")
        return message_id

    if mode == "sendmail":
        path = cfg.get("sendmail_path") or settings.sendmail_path or "/usr/sbin/sendmail"
        if not os.access(path, os.X_OK):
            raise FileNotFoundError(f"sendmail не найден или не исполняемый: {path}")
        _assert_mta_alive()
        cmd = [path, "-t", "-oi", "-f", cfg["mail_from"]]
        proc = subprocess.run(
            cmd, input=msg.as_bytes(), capture_output=True, timeout=60, check=False)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or b"").decode(errors="replace").strip()
            raise RuntimeError(err or f"sendmail exit {proc.returncode}")
        _assert_mta_alive()
        return message_id

    # smtp
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
