"""Клиент HTTP-сервиса 1С (pull) — см. db/1c_contract.md.

Config-gated: если ONEC_BASE_URL пуст, configured()=False и никто из 1С не тянет
(дев/тесты работают на push-фидах /feeds/*). Транспорт вынесен (_transport) для
тестируемости — self-check подменяет его фейком без сети.
"""
from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request

from app.config import settings


def configured() -> bool:
    return bool(settings.onec_base_url)


def _http_sync(method: str, path: str, params: dict | None, body: dict | None) -> dict:
    url = settings.onec_base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if settings.onec_token:
        headers["Authorization"] = f"Bearer {settings.onec_token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# Точка подмены в тестах: (method, path, params, body) -> dict.
_transport = None


async def _call(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    if _transport is not None:
        return _transport(method, path, params, body)
    return await asyncio.to_thread(_http_sync, method, path, params, body)


# --- Эндпоинты 1С ---

async def fetch_catalog(changed_since: str | None = None, page: int = 1, page_size: int = 1000) -> dict:
    params = {"page": page, "page_size": page_size}
    if changed_since:
        params["changed_since"] = changed_since
    return await _call("GET", "/catalog", params)


async def fetch_products(ids: list[str]) -> dict:
    return await _call("POST", "/products", body={"ids": ids})


async def fetch_cart(session_id: str) -> dict:
    return await _call("GET", "/cart", {"session_id": session_id})


async def order_exists(user_id: str, since: str) -> dict:
    return await _call("GET", "/orders/exists", {"user_id": user_id, "since": since})


# --- Маппинг товара 1С -> строка products ---

def map_product(p: dict) -> tuple:
    return (p["product_id"], p["name"], p["price"], p.get("image_url"),
            p["category_id"], p["product_url"], bool(p.get("in_stock", True)))


_UPSERT_PRODUCT = """
INSERT INTO products(product_id, name, price, image_url, category_id, product_url, in_stock, updated_at)
VALUES($1, $2, $3, $4, $5, $6, $7, now())
ON CONFLICT (product_id) DO UPDATE SET
  name=EXCLUDED.name, price=EXCLUDED.price, image_url=EXCLUDED.image_url,
  category_id=EXCLUDED.category_id, product_url=EXCLUDED.product_url,
  in_stock=EXCLUDED.in_stock, updated_at=now()"""


async def _ensure_categories(con, items: list[dict]) -> None:
    """Заводим новые категории из каталога (sort_order — по порядку появления)."""
    cats = {}
    for p in items:
        cats.setdefault(p["category_id"], p.get("category_name", p["category_id"]))
    existing = {r["category_id"] for r in await con.fetch("SELECT category_id FROM categories")}
    new = [(cid, name) for cid, name in cats.items() if cid not in existing]
    if not new:
        # обновим названия существующих
        await con.executemany("UPDATE categories SET name=$2 WHERE category_id=$1",
                              [(c, n) for c, n in cats.items()])
        return
    start = (await con.fetchval("SELECT COALESCE(MAX(sort_order), 0) FROM categories")) or 0
    await con.executemany(
        "INSERT INTO categories(category_id, name, sort_order) VALUES($1, $2, $3)",
        [(cid, name, start + i + 1) for i, (cid, name) in enumerate(new)],
    )


async def sync_catalog(con, changed_since: str | None = None) -> int:
    """Тянет каталог из 1С постранично и апсертит в products. Возвращает число товаров."""
    if not configured():
        return 0
    total, page = 0, 1
    while True:
        data = await fetch_catalog(changed_since=changed_since, page=page)
        items = data.get("items", [])
        if not items:
            break
        await _ensure_categories(con, items)
        await con.executemany(_UPSERT_PRODUCT, [map_product(p) for p in items])
        total += len(items)
        page_size = data.get("page_size", len(items))
        if page * page_size >= data.get("total", total):
            break
        page += 1
    return total


def _demo() -> None:
    """Self-check маппинга и пагинации (без сети — через фейковый транспорт)."""
    global _transport
    assert map_product({"product_id": "1", "name": "A", "price": 10.0,
                        "category_id": "c", "product_url": "u"}) == ("1", "A", 10.0, None, "c", "u", True)
    # Пагинация: транспорт отдаёт 2 страницы по 1 товару, total=2.
    pages = {
        1: {"items": [{"product_id": "1", "name": "A", "price": 10, "category_id": "c",
                       "category_name": "Cat", "product_url": "u", "in_stock": True}],
            "page": 1, "page_size": 1, "total": 2},
        2: {"items": [{"product_id": "2", "name": "B", "price": 20, "category_id": "c",
                       "category_name": "Cat", "product_url": "u2", "in_stock": True}],
            "page": 2, "page_size": 1, "total": 2},
    }
    seen = []

    def fake(method, path, params, body):
        seen.append((method, path, params.get("page") if params else None))
        return pages[params["page"]]
    _transport = fake
    # Проверяем, что fetch_catalog проходит обе страницы.
    async def run():
        p1 = await fetch_catalog(page=1)
        p2 = await fetch_catalog(page=2)
        return p1["items"][0]["product_id"], p2["items"][0]["product_id"]
    a, b = asyncio.run(run())
    assert (a, b) == ("1", "2"), (a, b)
    _transport = None
    print("onec._demo OK")


if __name__ == "__main__":
    _demo()
