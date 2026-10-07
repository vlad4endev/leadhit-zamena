"""Отправка через sendmail / SMTP-relay + обратный вызов событий в основное приложение."""
from __future__ import annotations

import asyncio
import json
import os
import smtplib
import socket
import ssl
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


# Таймаут на ОДНУ попытку connect к IP. Полный timeout SMTP (30с) на каждый A/AAAA
# давал 1–2 минуты, если первые адреса Яндекса недоступны с VPS.
_CONNECT_TRY_SEC = 5.0


def _connect_ipv4_first(host: str, port: int, timeout: float) -> socket.socket:
    """TCP с приоритетом IPv4.

    В Docker bridge часто нет IPv6-маршрута: getaddrinfo отдаёт AAAA первым →
    OSError 101 Network is unreachable, хотя с хоста тот же SMTP доступен по IPv4.
    """
    per_try = min(_CONNECT_TRY_SEC, float(timeout) if timeout else _CONNECT_TRY_SEC)
    errors: list[tuple[str, OSError]] = []
    for family in (socket.AF_INET, socket.AF_INET6):
        label = "IPv4" if family == socket.AF_INET else "IPv6"
        try:
            infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
        except socket.gaierror as e:
            errors.append((label, OSError(str(e))))
            continue
        for af, typ, proto, _, sockaddr in infos:
            sock = socket.socket(af, typ, proto)
            sock.settimeout(per_try)
            try:
                sock.connect(sockaddr)
                # Дальше SMTP-команды живут под общим timeout сессии.
                sock.settimeout(timeout)
                return sock
            except OSError as e:
                errors.append((f"{label} {sockaddr[0]}", e))
                sock.close()
    if errors:
        detail = "; ".join(f"{where}: {err}" for where, err in errors)
        last = errors[-1][1]
        raise OSError(f"cannot connect to {host}:{port} ({detail})") from last
    raise OSError(f"cannot connect to {host}:{port}")


class _SMTP(smtplib.SMTP):
    """SMTP, который сначала пробует IPv4 (см. _connect_ipv4_first)."""

    def _get_socket(self, host, port, timeout):
        return _connect_ipv4_first(host, port, timeout)


class _SMTP_SSL(smtplib.SMTP_SSL):
    """SMTP_SSL с IPv4-first + wrap_socket как в CPython."""

    def _get_socket(self, host, port, timeout):
        sock = _connect_ipv4_first(host, port, timeout)
        context = self.context if self.context is not None else ssl.create_default_context()
        return context.wrap_socket(sock, server_hostname=self._host or host)


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
    use_ssl = bool(cfg.get("smtp_ssl")) or int(cfg.get("smtp_port") or 0) == 465
    host, port = cfg["smtp_host"], int(cfg["smtp_port"])
    try:
        if use_ssl:
            with _SMTP_SSL(host, port, timeout=30) as s:
                if cfg["smtp_user"]:
                    s.login(cfg["smtp_user"], cfg["smtp_password"])
                s.send_message(msg)
        else:
            with _SMTP(host, port, timeout=30) as s:
                if cfg["smtp_starttls"]:
                    s.starttls()
                if cfg["smtp_user"]:
                    s.login(cfg["smtp_user"], cfg["smtp_password"])
                s.send_message(msg)
    except TimeoutError as e:
        raise TimeoutError(
            f"нет ответа от {host}:{port} за 30с "
            f"(SSL={use_ssl}). Часто VPS режет исходящий SMTP — "
            f"проверьте: docker compose exec mailer "
            f"python -c \"import socket; socket.create_connection(('{host}',{port}),5)\""
        ) from e
    except OSError as e:
        if getattr(e, "errno", None) == 101 or "unreachable" in str(e).lower():
            raise OSError(
                f"[Errno 101] Network is unreachable → {host}:{port}. "
                f"С хоста SMTP доступен, из контейнера нет (часто IPv6 без маршрута). "
                f"Обновите mailer (IPv4-first) или проверьте: "
                f"docker compose exec mailer python -c "
                f"\"import socket; print(socket.getaddrinfo('{host}',{port}))\""
            ) from e
        raise
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
        # Короткий timeout: callback не должен тормозить очередь (раньше до 15с на письмо).
        urllib.request.urlopen(req, timeout=3).read()
    except Exception as e:  # noqa: BLE001 — доставка события best-effort
        print(f"[callback-fail] {type(e).__name__}: {e}")


async def send(to, subject, html, from_email, from_name) -> str:
    return await asyncio.to_thread(send_sync, to, subject, html, from_email, from_name)


async def callback(meta: dict, event: str) -> None:
    """Пробрасывает событие доставки в основное приложение (meta + event)."""
    await asyncio.to_thread(_callback_sync, {**meta, "event": event})
