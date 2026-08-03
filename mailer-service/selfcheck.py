"""Self-check: очередь (enqueue→due→mark_sent) и dev-отправитель. Без сети и SMTP.

Запуск: python selfcheck.py
"""
from __future__ import annotations

import asyncio
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test_outbox.db")

from app import sender, store  # noqa: E402


async def main() -> None:
    await store.init()
    mid = await store.enqueue("u@example.com", "Тема", "<b>hi</b>", "", "", {"log_id": 42})
    due = await store.due(10)
    assert len(due) == 1 and due[0]["id"] == mid, due
    assert await store.stats() == {"queued": 1}

    message_id = sender.send_sync("u@example.com", "Тема", "<b>hi</b>", "", "")  # dev-режим
    assert message_id.startswith("<") and "@" in message_id, message_id

    await store.mark_sent(mid, message_id)
    assert (await store.get(mid))["state"] == "sent"
    assert await store.due(10) == []            # отправленное не выбирается
    assert await store.stats() == {"sent": 1}

    row = await store.by_message_id(message_id)
    assert row and row["id"] == mid
    print("mailer-service selfcheck OK")


if __name__ == "__main__":
    asyncio.run(main())
