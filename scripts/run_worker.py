"""Прогон воркеров сценариев (один тик). В проде вызывается по cron / в цикле.

Запуск: python scripts/run_worker.py postsale
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analytics, best_offer, cart, db, postsale


async def main(service: str) -> None:
    await db.connect()
    try:
        async with db.pool().acquire() as con:
            if service == "postsale":
                n = await postsale.run_due(con)
                print(f"postsale: sent={n}")
            elif service == "best_offer":
                n = await best_offer.run_batch(con)
                print(f"best_offer: sent={n}")
            elif service == "cart":
                n = await cart.run_due(con)
                print(f"cart: sent={n}")
            elif service == "attribution":
                n = await analytics.run_attribution(con)
                print(f"attribution: linked={n}")
            else:
                raise SystemExit(f"unknown service: {service}")
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "postsale"))
