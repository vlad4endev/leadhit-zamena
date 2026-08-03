"""Фоновый воркер: разбирает outbox, шлёт с рейт-лимитом и ретраями.

Рейт-лимит: интервал между отправками = 60 / rate_per_min (прогрев домена, антифлуд).
Ретраи: экспоненциальный бэкофф до max_attempts, потом state=failed.
"""
from __future__ import annotations

import asyncio
import json
import time

from app.config import settings
from app import sender, store


async def _process(msg: dict) -> None:
    meta = json.loads(msg["meta"] or "{}")
    try:
        message_id = await sender.send(
            msg["to_addr"], msg["subject"], msg["html"], msg["from_email"], msg["from_name"])
        await store.mark_sent(msg["id"], message_id)
        await sender.callback(meta, "sent")   # событие «отправлено» в основное приложение
    except Exception as e:  # noqa: BLE001 — сбой отправки → ретрай/фейл
        attempts = msg["attempts"] + 1
        if attempts >= settings.max_attempts:
            await store.mark_failed(msg["id"], attempts)
            await sender.callback(meta, "failed")
            print(f"[send-failed] {msg['id']} to={msg['to_addr']} {type(e).__name__}: {e}")
        else:
            backoff = settings.retry_base_sec * (2 ** (attempts - 1))
            await store.mark_retry(msg["id"], attempts, time.time() + backoff)
            print(f"[send-retry] {msg['id']} attempt={attempts} in {backoff}s: {e}")


async def run() -> None:
    print("mailer-worker: старт")
    while True:
        rate = (await store.get_config())["rate_per_min"]   # актуальный рейт-лимит из конфига
        interval = 60 / max(rate, 1)
        batch = await store.due(limit=20)
        if not batch:
            await asyncio.sleep(1)
            continue
        for msg in batch:
            await _process(msg)
            await asyncio.sleep(interval)   # рейт-лимит между отправками
