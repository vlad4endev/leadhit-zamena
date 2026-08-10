"""Импорт справочных данных из кастомного XML: каталог, топ-5, подписчики.

Заменяет живой pull каталога из 1С (заказы остаются на живой 1С). Схема — наша,
описана в db/import_contract.md. Секции независимы: импортируем те, что есть в файле.
Кодировка берётся из XML-декларации (utf-8 или windows-1251) — ET.fromstring по bytes.

Upsert-логика переиспользуется из feeds.py (та же схема БД); категории заводим через
onec._ensure_categories (корректно раздаёт sort_order новым, не ломая UNIQUE).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from app import onec
from app.feeds import (
    Product, Subscriber, Top5Row,
    upsert_products_rows, upsert_subscribers_rows, upsert_top5_rows,
)

MAX_BYTES = 50 * 1024 * 1024  # ~5к товаров помещаются с запасом; защита trust-boundary
_TRUE = {"1", "true", "yes", "да", "истина"}


def _text(el, tag: str, default=None):
    c = el.find(tag)
    return c.text.strip() if c is not None and c.text and c.text.strip() else default


def _bool(s, default: bool) -> bool:
    return default if s is None else s.strip().lower() in _TRUE


def parse(data: bytes) -> dict:
    """XML bytes → {'categories':[{category_id,category_name}], 'products':[Product],
    'top5':[Top5Row], 'subscribers':[Subscriber]}. ValueError на кривом файле."""
    if not data:
        raise ValueError("пустой файл")
    if len(data) > MAX_BYTES:
        raise ValueError(f"файл больше {MAX_BYTES // (1024 * 1024)} МБ")
    try:
        root = ET.fromstring(data)  # уважает encoding из XML-декларации
    except ET.ParseError as e:
        raise ValueError(f"битый XML: {e}")
    if root.tag == "yml_catalog":
        # Стандартная выгрузка магазина (Яндекс.Маркет). Отдельной кнопки в админке нет
        # намеренно: корень однозначен, поэтому та же «Импорт из файла» принимает оба
        # формата, а дальше идёт общий import_all.
        from app import import_yml
        return import_yml.parse_root(root)
    if root.tag != "grosterhit-import":
        raise ValueError(
            f"ожидался корень <grosterhit-import> или <yml_catalog>, получен <{root.tag}>")

    categories: dict[str, str] = {}   # category_id -> name (dedup, порядок появления)
    products: list[Product] = []
    top5: list[Top5Row] = []
    subscribers: list[Subscriber] = []

    cat = root.find("catalog")
    if cat is not None:
        cats_el = cat.find("categories")
        if cats_el is not None:
            for c in cats_el.findall("category"):
                cid = (c.get("id") or "").strip()
                if not cid:
                    raise ValueError("<category> без атрибута id")
                categories.setdefault(cid, (c.text or cid).strip())
        prods_el = cat.find("products")
        if prods_el is not None:
            for p in prods_el.findall("product"):
                pid = _text(p, "id")
                if not pid:
                    raise ValueError("<product> без <id>")
                price_s = _text(p, "price", "0")
                try:
                    price = float(price_s)
                except ValueError:
                    raise ValueError(f"product {pid}: цена не число ({price_s!r})")
                cid = _text(p, "category", "")
                if not cid:
                    raise ValueError(f"product {pid}: пустая <category>")
                categories.setdefault(cid, cid)  # гарантия FK: категория товара точно есть
                products.append(Product(
                    product_id=pid,
                    name=_text(p, "name", pid),
                    price=price,
                    image_url=_text(p, "image_url"),
                    category_id=cid,
                    product_url=_text(p, "product_url", ""),
                    in_stock=_bool(_text(p, "in_stock"), True),
                ))

    t5 = root.find("top5")
    if t5 is not None:
        for c in t5.findall("category"):
            cid = (c.get("id") or "").strip()
            if not cid:
                raise ValueError("<top5>/<category> без атрибута id")
            for pr in c.findall("product"):
                try:
                    pos = int(pr.get("position", "0"))
                except ValueError:
                    raise ValueError(f"top5 {cid}: position не число")
                pid = (pr.text or "").strip()
                if not pid or not (1 <= pos <= 5):
                    raise ValueError(f"top5 {cid}: нужен product_id и position 1..5")
                top5.append(Top5Row(category_id=cid, position=pos, product_id=pid))

    subs = root.find("subscribers")
    if subs is not None:
        for s in subs.findall("subscriber"):
            uid = _text(s, "user_id")
            if not uid:
                raise ValueError("<subscriber> без <user_id>")
            subscribers.append(Subscriber(
                user_id=uid,
                email=_text(s, "email"),
                is_unsubscribed=_bool(_text(s, "is_unsubscribed"), False),
                consent_at=_text(s, "consent_at"),
                last_purchase_at=_text(s, "last_purchase_at"),
                last_purchase_category_id=_text(s, "last_purchase_category_id"),
            ))

    cat_list = [{"category_id": cid, "category_name": name} for cid, name in categories.items()]
    return {"categories": cat_list, "products": products, "top5": top5, "subscribers": subscribers}


async def import_all(con, parsed: dict) -> dict:
    """Атомарный импорт присутствующих секций. Порядок: categories→products→top5 (FK)."""
    counts: dict[str, int] = {}
    async with con.transaction():
        if parsed["categories"]:
            await onec._ensure_categories(con, parsed["categories"])
            counts["categories"] = len(parsed["categories"])
        if parsed["products"]:
            await upsert_products_rows(con, parsed["products"])
            counts["products"] = len(parsed["products"])
        if parsed["top5"]:
            await upsert_top5_rows(con, parsed["top5"])
            counts["top5"] = len(parsed["top5"])
        if parsed["subscribers"]:
            await upsert_subscribers_rows(con, parsed["subscribers"])
            counts["subscribers"] = len(parsed["subscribers"])
    return counts


_SAMPLE = b"""<?xml version="1.0" encoding="utf-8"?>
<grosterhit-import>
  <catalog>
    <categories>
      <category id="salfetki-bumajnye" sort="1">\xd0\x91\xd1\x83\xd0\xbc\xd0\xb0\xd0\xb6\xd0\xbd\xd1\x8b\xd0\xb5 \xd1\x81\xd0\xb0\xd0\xbb\xd1\x84\xd0\xb5\xd1\x82\xd0\xba\xd0\xb8</category>
    </categories>
    <products>
      <product>
        <id>0057412</id><name>\xd0\xa1\xd0\xb0\xd0\xbb\xd1\x84\xd0\xb5\xd1\x82\xd0\xba\xd0\xb0</name>
        <category>salfetki-bumajnye</category><price>71.60</price><in_stock>true</in_stock>
        <product_url>https://groster.me/catalog/salfetki-bumajnye/0057412/</product_url>
      </product>
      <product>
        <id>0038921</id><name>\xd0\x9a\xd0\xb0\xd1\x81\xd0\xb0\xd0\xbb\xd0\xb5\xd1\x82\xd0\xba\xd0\xb0</name>
        <category>kasaletki</category><price>11.80</price><in_stock>false</in_stock>
        <product_url>https://groster.me/catalog/kasaletki/0038921/</product_url>
      </product>
    </products>
  </catalog>
  <top5>
    <category id="salfetki-bumajnye">
      <product position="1">0057412</product>
    </category>
  </top5>
  <subscribers>
    <subscriber>
      <user_id>42817</user_id><email>m.orlova@mail.ru</email>
      <is_unsubscribed>false</is_unsubscribed><consent_at>2026-07-20T10:00:00+03:00</consent_at>
    </subscriber>
  </subscribers>
</grosterhit-import>"""


def _demo() -> None:
    """Self-check парсера на встроенном примере (без сети/БД)."""
    r = parse(_SAMPLE)
    # 2 явных категории? Нет: 1 явная + kasaletki добавлена из товара (гарантия FK) = 2.
    assert {c["category_id"] for c in r["categories"]} == {"salfetki-bumajnye", "kasaletki"}, r["categories"]
    assert len(r["products"]) == 2 and r["products"][0].price == 71.6
    assert r["products"][1].in_stock is False
    assert len(r["top5"]) == 1 and r["top5"][0].position == 1
    assert len(r["subscribers"]) == 1 and r["subscribers"][0].email == "m.orlova@mail.ru"

    # Битый XML и неверный корень — понятная ошибка, не краш.
    for bad, why in [(b"<x>", "битый"), (b"<other/>", "корень"), (b"", "пустой")]:
        try:
            parse(bad); assert False, f"должно упасть: {why}"
        except ValueError:
            pass
    print("import_xml._demo OK")


if __name__ == "__main__":
    _demo()
