"""Self-check: очередь (enqueue→due→mark_sent), dev-отправитель и SSL-флаг конфига.

Запуск: python selfcheck.py
"""
from __future__ import annotations

import asyncio
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test_outbox.db")
os.environ.pop("SMTP_HOST", None)
os.environ["MAIL_TRANSPORT"] = ""

from app import sender, store  # noqa: E402
from app.config import settings  # noqa: E402


async def main() -> None:
    await store.init()
    mid = await store.enqueue("u@example.com", "Тема", "<b>hi</b>", "", "", {"log_id": 42})
    due = await store.due(10)
    assert len(due) == 1 and due[0]["id"] == mid, due
    assert await store.stats() == {"queued": 1}

    assert sender.provider_name() == "dev"
    message_id = sender.send_sync("u@example.com", "Тема", "<b>hi</b>", "", "")  # dev-режим
    assert message_id.startswith("<") and "@" in message_id, message_id

    await store.mark_sent(mid, message_id)
    assert (await store.get(mid))["state"] == "sent"
    assert await store.due(10) == []            # отправленное не выбирается
    assert await store.stats() == {"sent": 1}

    row = await store.by_message_id(message_id)
    assert row and row["id"] == mid

    # Конфиг SMTP/SSL сохраняется и читается (без реальной сети).
    await store.set_config({
        "mail_transport": "smtp",
        "smtp_host": "smtp.yandex.ru",
        "smtp_port": "465",
        "smtp_ssl": "true",
        "smtp_starttls": "false",
        "smtp_user": "box@yandex.ru",
        "mail_from": "box@yandex.ru",
    })
    cfg = await store.get_config()
    assert cfg["smtp_host"] == "smtp.yandex.ru"
    assert cfg["smtp_port"] == 465
    assert cfg["smtp_ssl"] is True
    assert cfg["smtp_starttls"] is False
    assert sender.provider_name(cfg) == "smtp"

    # IPv4-first helper exists (unit, без сети).
    assert callable(sender._connect_ipv4_first)

    # diagnostics: без host → ok=None; callback пуст → ok=False (без сети).
    from app import diagnostics
    smtp_empty = diagnostics.check_smtp({"smtp_host": "", "smtp_port": 0})
    assert smtp_empty.get("ok") is None, smtp_empty
    cb = diagnostics.check_callback()
    assert cb.get("ok") is False and "пуст" in (cb.get("detail") or ""), cb
    assert callable(diagnostics.warn_on_startup)
    assert callable(diagnostics.run_all)

    print("mailer-service selfcheck OK")


if __name__ == "__main__":
    asyncio.run(main())
