"""Постоянный процесс воркеров. Гоняет все сценарии на своих интервалах в одном loop.

Прод-режим: Корзина — near-real-time (частый тик), Постпродажа/атрибуция — периодически,
Best Offer — раз/сутки. Альтернатива — отдельные cron-джобы на scripts/run_worker.py.

Запуск: python scripts/worker_loop.py
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import activity_log, analytics, app_settings, best_offer, cart, db, onec, postsale  # noqa: E402

# Источник в activity_log: сценарии пишем своим source, остальное — worker.
_SOURCE = {"cart": "cart", "postsale": "postsale", "best_offer": "best_offer",
           "attribution": "worker", "catalog_1c": "onec"}


async def _loop(name: str, interval: int, fn) -> None:
    while True:
        try:
            async with db.pool().acquire() as con:
                result = await fn(con)
            if result:
                print(f"[{name}] {result}")
        except Exception as e:  # noqa: BLE001 — воркер не должен падать целиком из-за одной ошибки
            print(f"[{name}] ERROR {type(e).__name__}: {e}")
            try:
                await activity_log.write(
                    level="error", source=_SOURCE.get(name, "worker"), event="worker_error",
                    service=name if name in ("best_offer", "cart", "postsale") else None,
                    message=f"воркер {name}: {type(e).__name__}: {e}",
                    details={"worker": name, "error_type": type(e).__name__, "error": str(e)[:800]},
                )
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(interval)


async def main() -> None:
    await db.connect()
    # Интервалы из настроек БД (поверх .env). Правки применяются при перезапуске воркеров.
    async with db.pool().acquire() as con:
        cfg = await app_settings.get(con)
        await onec.load_overrides(con)  # base_url/token из админки (app_config) поверх .env
    print("worker_loop: старт "
          f"(cart={cfg['cart_tick_sec']}s, postsale={cfg['postsale_tick_sec']}s, "
          f"attribution={cfg['attribution_tick_sec']}s, best_offer={cfg['best_offer_tick_sec']}s)")
    await activity_log.write(
        level="info", source="worker", event="worker_start",
        message="воркеры запущены",
        details={"cart_tick_sec": cfg["cart_tick_sec"], "postsale_tick_sec": cfg["postsale_tick_sec"],
                 "attribution_tick_sec": cfg["attribution_tick_sec"],
                 "best_offer_tick_sec": cfg["best_offer_tick_sec"],
                 "onec": onec.configured()},
    )
    # Чистим старые диагностические записи при старте (retention).
    await activity_log.prune()

    loops = [
        _loop("cart", cfg["cart_tick_sec"], cart.run_due),
        _loop("postsale", cfg["postsale_tick_sec"], postsale.run_due),
        _loop("attribution", cfg["attribution_tick_sec"], analytics.run_attribution),
        _loop("best_offer", cfg["best_offer_tick_sec"], best_offer.run_batch),
    ]
    if onec.configured():  # синхронизация каталога из 1С (pull)
        loops.append(_loop("catalog_1c", cfg["onec_sync_tick_sec"], onec.sync_catalog))
    tasks = asyncio.gather(*loops)
    # Мягкая остановка по SIGTERM (systemd) и SIGINT (Ctrl+C).
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, tasks.cancel)
    try:
        await tasks
    except asyncio.CancelledError:
        print("worker_loop: остановка")
    finally:
        await db.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("worker_loop: остановлен")
