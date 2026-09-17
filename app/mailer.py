"""Отправка писем. Абстракция + sendmail + SMTP-relay + внешний mailer-service.

Приоритет get_mailer():
  mailer-service (если URL) → sendmail (MAIL_TRANSPORT=sendmail) → SMTP → dev-лог.
Воркеры зависят только от Mailer.send() / send_batch().
"""
from __future__ import annotations

import asyncio
import json
import os
import smtplib
import subprocess
import urllib.request
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Optional, Protocol

from app.config import settings


def _normalize_message(m: dict) -> dict:
    """Приводит элемент batch к полям mailer-service."""
    return {
        "to": str(m.get("to") or "").strip(),
        "subject": str(m.get("subject") or ""),
        "html": str(m.get("html") or ""),
        "from_email": str(m.get("from_email") or ""),
        "from_name": str(m.get("from_name") or ""),
        "meta": m.get("meta") if isinstance(m.get("meta"), dict) else {},
    }


def _build_message(to: str, subject: str, html: str,
                   from_email: str, from_name: str) -> EmailMessage:
    """Собирает MIME. From = mail_from (envelope), сценарий → Reply-To."""
    msg = EmailMessage()
    domain = (settings.mail_from.split("@")[-1] if "@" in settings.mail_from else "localhost")
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["From"] = f"{from_name or settings.mail_from_name} <{settings.mail_from}>"
    if from_email and from_email != settings.mail_from:
        msg["Reply-To"] = from_email
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("Для просмотра письма включите HTML.")
    msg.add_alternative(html, subtype="html")
    return msg


class Mailer(Protocol):
    async def send(self, to: str, subject: str, html: str,
                   from_email: str = "", from_name: str = "", meta: Optional[dict] = None) -> bool:
        ...

    async def send_batch(self, messages: list[dict]) -> dict:
        """Массовая постановка/отправка. Возвращает {queued|sent, failed, ids?}."""
        ...


async def _send_batch_loop(mailer: Mailer, messages: list[dict]) -> dict:
    sent = 0
    failed = 0
    for raw in messages:
        m = _normalize_message(raw)
        if not m["to"]:
            failed += 1
            continue
        ok = await mailer.send(m["to"], m["subject"], m["html"],
                               m["from_email"], m["from_name"], m["meta"])
        if ok:
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed, "queued": sent}


class LogMailer:
    """Dev-отправитель: печатает письмо в лог, всегда 'успех'. Для локали и тестов."""

    async def send(self, to: str, subject: str, html: str,
                   from_email: str = "", from_name: str = "", meta: Optional[dict] = None) -> bool:
        print(f"[MAIL] from={from_email or settings.mail_from} to={to} subject={subject!r} html_len={len(html)}")
        return True

    async def send_batch(self, messages: list[dict]) -> dict:
        return await _send_batch_loop(self, messages)


def _assert_mta_alive() -> None:
    """sendmail часто возвращает 0 даже при остановленном postfix — ловим «mail system is down»."""
    postqueue = "/usr/sbin/postqueue"
    if not os.access(postqueue, os.X_OK):
        postqueue = "postqueue"
    try:
        q = subprocess.run(
            [postqueue, "-p"], capture_output=True, timeout=10, check=False)
    except FileNotFoundError:
        return  # не postfix — не проверяем
    except Exception:  # noqa: BLE001
        return
    err = (q.stderr or q.stdout or b"").decode(errors="replace").lower()
    if q.returncode != 0 and ("mail system is down" in err or "unavailable" in err):
        raise RuntimeError(
            "sendmail принял письмо, но MTA не запущен (postqueue: mail system is down). "
            "Запустите postfix/exim на сервере и повторите тест.")


class SendmailMailer:
    """Локальный MTA через sendmail-совместимый бинарь (postfix/exim/ssmtp: /usr/sbin/sendmail -t)."""

    async def send(self, to: str, subject: str, html: str,
                   from_email: str = "", from_name: str = "", meta: Optional[dict] = None) -> bool:
        try:
            await asyncio.to_thread(self._send_sync, to, subject, html, from_email, from_name)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[SENDMAIL-ERROR] to={to} {type(e).__name__}: {e}")
            return False

    async def send_batch(self, messages: list[dict]) -> dict:
        return await _send_batch_loop(self, messages)

    def _send_sync(self, to: str, subject: str, html: str, from_email: str, from_name: str) -> None:
        path = settings.sendmail_path or "/usr/sbin/sendmail"
        if not os.access(path, os.X_OK):
            raise FileNotFoundError(f"sendmail не найден или не исполняемый: {path}")

        _assert_mta_alive()

        msg = _build_message(to, subject, html, from_email, from_name)
        # -t: получатели из заголовков; -oi: точка в теле не конец письма; -f: envelope sender
        cmd = [path, "-t", "-oi", "-f", settings.mail_from]
        proc = subprocess.run(
            cmd, input=msg.as_bytes(), capture_output=True, timeout=60, check=False)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or b"").decode(errors="replace").strip()
            raise RuntimeError(err or f"sendmail exit {proc.returncode}")

        _assert_mta_alive()


class HttpMailer:
    """Отправка через mailer-service: POST /v1/send и /v1/send/batch."""

    async def send(self, to: str, subject: str, html: str,
                   from_email: str = "", from_name: str = "", meta: Optional[dict] = None) -> bool:
        body = {"to": to, "subject": subject, "html": html,
                "from_email": from_email, "from_name": from_name, "meta": meta or {}}
        try:
            await asyncio.to_thread(self._post, "/v1/send", body)
            return True
        except Exception as e:  # noqa: BLE001 — недоступность сервиса логируем, письмо не теряем
            print(f"[MAILER-SVC-ERROR] to={to} {type(e).__name__}: {e}")
            return False

    async def send_batch(self, messages: list[dict]) -> dict:
        normalized = []
        skipped = 0
        for raw in messages:
            m = _normalize_message(raw)
            if not m["to"]:
                skipped += 1
                continue
            normalized.append(m)
        if not normalized:
            return {"queued": 0, "failed": skipped, "ids": []}
        try:
            res = await asyncio.to_thread(self._post, "/v1/send/batch", {"messages": normalized})
            return {
                "queued": int(res.get("queued") or len(normalized)),
                "failed": skipped,
                "ids": res.get("ids") or [],
            }
        except Exception as e:  # noqa: BLE001
            print(f"[MAILER-SVC-BATCH-ERROR] n={len(normalized)} {type(e).__name__}: {e}")
            return {"queued": 0, "failed": skipped + len(normalized), "ids": [],
                    "error": f"{type(e).__name__}: {e}"}

    def _post(self, path: str, body: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if settings.mailer_service_token:
            headers["Authorization"] = f"Bearer {settings.mailer_service_token}"
        timeout = 60 if path.endswith("/batch") else 15
        req = urllib.request.Request(
            settings.mailer_service_url.rstrip("/") + path,
            data=json.dumps(body).encode(), method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}


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

    async def send_batch(self, messages: list[dict]) -> dict:
        return await _send_batch_loop(self, messages)

    def _send_sync(self, to: str, subject: str, html: str, from_email: str, from_name: str) -> None:
        msg = _build_message(to, subject, html, from_email, from_name)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            if settings.smtp_starttls:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)


def _wants_sendmail() -> bool:
    t = (settings.mail_transport or "").strip().lower()
    if t == "sendmail":
        return True
    if t == "smtp":
        return False
    # авто: sendmail, если бинарь есть и SMTP не задан
    if settings.smtp_host:
        return False
    path = settings.sendmail_path or "/usr/sbin/sendmail"
    return os.access(path, os.X_OK)


def get_mailer() -> Mailer:
    # Приоритет: mailer-service → sendmail → SMTP-relay → dev-лог.
    if settings.mailer_service_url:
        return HttpMailer()
    if _wants_sendmail():
        return SendmailMailer()
    if settings.smtp_host:
        return SmtpMailer()
    return LogMailer()
