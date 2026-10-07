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
    t0 = time.monotonic()
    try:
        message_id = await sender.send(
            msg["to_addr"], msg["subject"], msg["html"], msg["from_email"], msg["from_name"])
        await store.mark_sent(msg["id"], message_id)
        # Callback вне критического пути отправки: ошибка/тормоза API не копят очередь.
        try:
            await sender.callback(meta, "sent")
        except Exception as ce:  # noqa: BLE001
            print(f"[callback-fail] {msg['id']} {type(ce).__name__}: {ce}")
        dt = time.monotonic() - t0
        if dt > 5:
            print(f"[send-slow] {msg['id']} to={msg['to_addr']} {dt:.1f}s")
    except Exception as e:  # noqa: BLE001 — сбой отправки → ретрай/фейл
        attempts = msg["attempts"] + 1
        if attempts >= settings.max_attempts:
            await store.mark_failed(msg["id"], attempts)
            try:
                await sender.callback(meta, "failed")
            except Exception:  # noqa: BLE001
                pass
            print(f"[send-failed] {msg['id']} to={msg['to_addr']} {type(e).__name__}: {e}")
        else:
            backoff = settings.retry_base_sec * (2 ** (attempts - 1))
            await store.mark_retry(msg["id"], attempts, time.time() + backoff)
            print(f"[send-retry] {msg['id']} attempt={attempts} in {backoff}s: {e}")


async def run() -> None:
    print("mailer-worker: старт")
    while True:
        rate = int((await store.get_config())["rate_per_min"] or 120)
        # Защита от «1 письмо / 2 мин»: слишком маленький rate в админке.
        if rate < 1:
            rate = 1
        interval = 60.0 / rate
        batch = await store.due(limit=20)
        if not batch:
            await asyncio.sleep(0.5)
            continue
        for i, msg in enumerate(batch):
            await _process(msg)
            # Пауза только МЕЖДУ письмами, не после последнего в пачке.
            if i + 1 < len(batch) and interval > 0:
                await asyncio.sleep(interval)
