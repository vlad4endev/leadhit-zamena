"""Импорт каталога из JSON-выгрузки товаров (плоский массив продуктов).

Формат — как в выгрузке сайта: массив объектов с product_id, category_id,
category_name, price, in_stock, image_url, product_url и доп. полями (unit,
currency, min_order_qty, attributes, tags, updated_at) — доп. поля игнорируем,
БД их не хранит. Категории берём из самих товаров (category_id -> category_name).

Возвращаем ту же структуру, что import_xml.parse, поэтому импорт делаем через
общий import_xml.import_all (тот же upsert, те же FK-гарантии). BOM/кодировка —
через utf-8-sig (выгрузка приходит с BOM).
"""
from __future__ import annotations

import json

from app.feeds import Product

MAX_BYTES = 50 * 1024 * 1024  # ~5к товаров с запасом; защита trust-boundary (как в XML-импорте)


def parse(data: bytes) -> dict:
    """JSON bytes → {'categories':[{category_id,category_name}], 'products':[Product],
    'top5':[], 'subscribers':[]}. ValueError на кривом файле."""
    if not data:
        raise ValueError("пустой файл")
    if len(data) > MAX_BYTES:
        raise ValueError(f"файл больше {MAX_BYTES // (1024 * 1024)} МБ")
    try:
        rows = json.loads(data.decode("utf-8-sig"))  # utf-8-sig: снимает BOM, читает обычный utf-8
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"битый JSON: {e}")
    if not isinstance(rows, list):
        raise ValueError("ожидался JSON-массив товаров")

    categories: dict[str, str] = {}   # category_id -> name (dedup, порядок появления)
    products: list[Product] = []
    for i, p in enumerate(rows):
        if not isinstance(p, dict):
            raise ValueError(f"элемент #{i}: ожидался объект товара")
        pid = str(p.get("product_id") or "").strip()
        if not pid:
            raise ValueError(f"элемент #{i}: пустой product_id")
        cid = str(p.get("category_id") or "").strip()
        if not cid:
            raise ValueError(f"товар {pid}: пустой category_id")
        price = p.get("price", 0)
        if not isinstance(price, (int, float)) or isinstance(price, bool):
            raise ValueError(f"товар {pid}: цена не число ({price!r})")
        cname = str(p.get("category_name") or "").strip() or cid
        categories.setdefault(cid, cname)  # гарантия FK: категория товара точно есть
        products.append(Product(
            product_id=pid,
            name=str(p.get("name") or pid),
            price=float(price),
            image_url=(p.get("image_url") or None),
            category_id=cid,
            product_url=(p.get("product_url") or ""),
            in_stock=bool(p.get("in_stock", True)),
        ))

    cat_list = [{"category_id": cid, "category_name": name} for cid, name in categories.items()]
    return {"categories": cat_list, "products": products, "top5": [], "subscribers": []}


_SAMPLE = (
    '﻿[\n'
    '  {"product_id": "0057412", "name": "Салфетка",'
    '   "category_id": "salfetki", "category_name": "Салфетки",'
    '   "price": 71.6, "in_stock": true, "product_url": "https://groster.me/x",'
    '   "image_url": "https://groster.me/x.jpg", "unit": "шт", "tags": ["Новинка"]},\n'
    '  {"product_id": "0038921", "name": "Касалетка",'
    '   "category_id": "kasaletki", "category_name": "",'
    '   "price": 12, "in_stock": false, "product_url": "", "image_url": ""}\n'
    ']'
).encode("utf-8")


def _demo() -> None:
    """Self-check парсера на встроенном примере с BOM (без сети/БД)."""
    r = parse(_SAMPLE)
    assert {c["category_id"] for c in r["categories"]} == {"salfetki", "kasaletki"}, r["categories"]
    # Пустой category_name падает обратно в category_id (гарантия NOT NULL name).
    assert dict((c["category_id"], c["category_name"]) for c in r["categories"])["kasaletki"] == "kasaletki"
    assert len(r["products"]) == 2 and r["products"][0].price == 71.6
    assert r["products"][1].in_stock is False
    assert r["products"][1].image_url is None and r["products"][1].product_url == ""
    assert r["top5"] == [] and r["subscribers"] == []

    for bad, why in [(b"{}", "не массив"), (b"[1,2]", "не объекты"), (b"", "пустой"), (b"[", "битый")]:
        try:
            parse(bad); assert False, f"должно упасть: {why}"
        except ValueError:
            pass
    print("import_json._demo OK")


if __name__ == "__main__":
    _demo()
