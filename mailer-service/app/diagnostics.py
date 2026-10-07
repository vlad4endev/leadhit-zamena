"""Проверки SMTP и callback — для /v1/diagnostics и стартовых предупреждений."""
from __future__ import annotations

import socket
from urllib.parse import urlparse

from app import sender, store
from app.config import settings


def _tcp(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        sock = sender._connect_ipv4_first(host, port, timeout)
        sock.close()
        return True, "ok"
    except OSError as e:
        return False, str(e)


def check_callback() -> dict:
    """TCP до CALLBACK_URL (api на loopback хоста при network_mode: host)."""
    url = (settings.callback_url or "").strip()
    if not url:
        return {"ok": False, "url": "", "detail": "CALLBACK_URL пуст"}
    u = urlparse(url)
    host = u.hostname or "127.0.0.1"
    if u.port:
        port = u.port
    else:
        port = 443 if u.scheme == "https" else 80
    ok, detail = _tcp(host, port, 3.0)
    return {"ok": ok, "url": url, "host": host, "port": port, "detail": detail}


def check_smtp(cfg: dict | None = None) -> dict:
    """TCP до SMTP (тот же путь, что и отправка). Без логина — только reachability."""
    cfg = cfg if cfg is not None else store.config_sync()
    host = (cfg.get("smtp_host") or settings.smtp_host or "").strip()
    try:
        port = int(cfg.get("smtp_port") or settings.smtp_port or 0)
    except (TypeError, ValueError):
        port = 0
    if not host or not port:
        return {"ok": None, "detail": "SMTP не настроен (dev/sendmail)"}
    ok, detail = _tcp(host, port, 5.0)
    return {"ok": ok, "host": host, "port": port, "detail": detail}


def run_all() -> dict:
    cfg = store.config_sync()
    smtp = check_smtp(cfg)
    callback = check_callback()
    provider = sender.provider_name(cfg)
    # overall: smtp None (не smtp) не валит; smtp False / callback False — degraded
    ok = True
    if smtp.get("ok") is False:
        ok = False
    if callback.get("ok") is False:
        ok = False
    if provider == "dev":
        ok = False
    return {
        "ok": ok,
        "provider": provider,
        "smtp": smtp,
        "callback": callback,
        "outbox": store.stats_sync(),
    }


def warn_on_startup() -> None:
    """Громкие предупреждения в лог при старте — чтобы не копить очередь вслепую."""
    cfg = store.config_sync()
    provider = sender.provider_name(cfg)
    if provider == "dev":
        print("[mailer-diag] WARN: provider=dev — письма не уйдут наружу "
              "(задайте SMTP в админке или SMTP_* в .env)")
    smtp = check_smtp(cfg)
    if smtp.get("ok") is False:
        print(f"[mailer-diag] WARN: SMTP недоступен "
              f"{smtp.get('host')}:{smtp.get('port')} — {smtp.get('detail')}")
    elif smtp.get("ok") is True:
        print(f"[mailer-diag] SMTP OK {smtp.get('host')}:{smtp.get('port')}")
    cb = check_callback()
    if cb.get("ok") is False:
        print(f"[mailer-diag] WARN: callback недоступен {cb.get('url')} — {cb.get('detail')}. "
              f"Проверьте API_HOST_PORT и что api слушает 127.0.0.1")
    else:
        print(f"[mailer-diag] callback TCP OK {cb.get('url')}")
