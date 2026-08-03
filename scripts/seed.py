"""Загрузка seed.json в БД через feed-эндпоинты. Порядок важен: FK-зависимости.

Запуск: приложение должно слушать на localhost:8099 (см. команду в тесте).
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"

with open("db/seed.json") as f:
    data = json.load(f)


def put(path: str, rows: list) -> None:
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(rows).encode(),
        method="PUT", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        print(path, "->", r.read().decode())


# Порядок: categories -> products -> top5 -> subscribers -> orders (FK).
put("/feeds/categories", data["categories"])
put("/feeds/products", data["products"])
put("/feeds/top5", data["top5"])
put("/feeds/subscribers", data["subscribers"])
put("/feeds/orders", data["orders"])
