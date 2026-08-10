"""Импорт каталога из YML (Yandex Market Language) — стандартной выгрузки магазина.

YML умеет генерировать любая CMS «из коробки» (тот же файл уходит в Яндекс.Маркет и
Директ), поэтому это самый дешёвый для витрины способ отдать нам ассортимент: отдельная
выгрузка под нас не нужна. Возвращаем ту же структуру, что import_xml.parse, и импорт
идёт общим import_xml.import_all — та же схема БД, те же FK-гарантии, та же атомарность.

Разбираем подмножество YML, которое ложится на нашу схему products:
  <offer id available> → product_id / in_stock, <name> (или vendor+model) → name,
  <price>, <categoryId>, <url> → product_url, первая <picture> → image_url.
Не храним oldprice/vendor/param: под них нет колонок, а письма их не используют.
Дерево категорий схлопываем в плоский список (parentId игнорируем) — рекомендации
работают по category_id товара, иерархия им не нужна.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from app.feeds import Product

MAX_BYTES = 50 * 1024 * 1024  # как в import_xml/import_json: защита trust-boundary
_TRUE = {"1", "true", "yes", "да", "истина"}


def _t(el, tag: str, default=None):
    """Текст дочернего тега или default. Пустой/пробельный считаем отсутствующим."""
    v = el.findtext(tag)
    v = v.strip() if v else ""
    return v or default


def _name(o, pid: str) -> str:
    """<name>, либо склейка typePrefix+vendor+model (выгрузки типа vendor.model)."""
    n = _t(o, "name")
    if n:
        return n
    parts = [_t(o, "typePrefix"), _t(o, "vendor"), _t(o, "model")]
    return " ".join(p for p in parts if p) or pid


def _price(o, pid: str) -> float:
    """Цена числом. Терпим запятую и разделители разрядов — их ставят реальные выгрузки."""
    s = _t(o, "price", "0")
    try:
        return float(s.replace(",", ".").replace(" ", "").replace("\xa0", ""))
    except ValueError:
        raise ValueError(f"offer {pid}: цена не число ({s!r})")


def _available(o) -> bool:
    """Наличие: атрибут available (стандарт) или элемент <available> у части выгрузок.
    Атрибут не указан вовсе → по спецификации YML товар считается доступным."""
    v = o.get("available")
    if v is None:
        v = _t(o, "available")
    return True if v is None else str(v).strip().lower() in _TRUE


def parse_root(root) -> dict:
    """Разобранный <yml_catalog> → та же структура, что возвращает import_xml.parse.

    top5/subscribers YML не несёт: топ-5 приходит отдельным фидом, подписчики — из 1С.
    """
    shop = root.find("shop")
    if shop is None:
        raise ValueError("<yml_catalog> без <shop>")

    categories: dict[str, str] = {}   # category_id -> name, в порядке появления
    cats_el = shop.find("categories")
    if cats_el is not None:
        for c in cats_el.findall("category"):
            cid = (c.get("id") or "").strip()
            if not cid:
                raise ValueError("<category> без атрибута id")
            categories[cid] = (c.text or "").strip() or cid

    products: list[Product] = []
    offers_el = shop.find("offers")
    for o in (offers_el.findall("offer") if offers_el is not None else []):
        pid = (o.get("id") or "").strip()
        if not pid:
            raise ValueError("<offer> без атрибута id")
        cid = _t(o, "categoryId", "")
        if not cid:
            raise ValueError(f"offer {pid}: пустой <categoryId>")
        categories.setdefault(cid, cid)  # гарантия FK: категория товара точно есть
        products.append(Product(
            product_id=pid,
            name=_name(o, pid),
            price=_price(o, pid),
            image_url=_t(o, "picture"),   # findtext берёт первую <picture> — она главная
            category_id=cid,
            product_url=_t(o, "url", ""),
            in_stock=_available(o),
        ))

    if not products:
        raise ValueError("в <offers> нет ни одного товара")

    cat_list = [{"category_id": cid, "category_name": name} for cid, name in categories.items()]
    return {"categories": cat_list, "products": products, "top5": [], "subscribers": []}


def parse(data: bytes) -> dict:
    """YML bytes → структура импорта. ValueError на кривом файле."""
    if not data:
        raise ValueError("пустой файл")
    if len(data) > MAX_BYTES:
        raise ValueError(f"файл больше {MAX_BYTES // (1024 * 1024)} МБ")
    try:
        root = ET.fromstring(data)  # уважает encoding из XML-декларации
    except ET.ParseError as e:
        raise ValueError(f"битый XML: {e}")
    if root.tag != "yml_catalog":
        raise ValueError(f"ожидался корень <yml_catalog>, получен <{root.tag}>")
    return parse_root(root)


_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<yml_catalog date="2026-08-11 09:00">
  <shop>
    <name>Гростер</name>
    <categories>
      <category id="10">Бумажные салфетки</category>
      <category id="11" parentId="10">Салфетки цветные</category>
    </categories>
    <offers>
      <offer id="0057412" available="true">
        <url>https://groster.me/catalog/salfetki-bumajnye/0057412/</url>
        <price>71.60</price>
        <oldprice>89.00</oldprice>
        <categoryId>10</categoryId>
        <picture>https://groster.me/img/0057412-1.jpg</picture>
        <picture>https://groster.me/img/0057412-2.jpg</picture>
        <name>Салфетка бумажная 24х24</name>
      </offer>
      <offer id="0038921" available="false" type="vendor.model">
        <price>11,80</price>
        <categoryId>12</categoryId>
        <vendor>Гростер</vendor>
        <model>Касалетка 100 мл</model>
      </offer>
    </offers>
  </shop>
</yml_catalog>""".encode()


def _demo() -> None:
    """Self-check парсера на встроенном примере (без сети/БД)."""
    r = parse(_SAMPLE)

    # Категории: 2 объявленных + 12 добавлена из товара (иначе upsert упал бы на FK).
    assert {c["category_id"] for c in r["categories"]} == {"10", "11", "12"}, r["categories"]
    assert r["categories"][0]["category_name"] == "Бумажные салфетки"

    p0, p1 = r["products"]
    assert p0.product_id == "0057412" and p0.price == 71.6
    assert p0.image_url.endswith("0057412-1.jpg"), "берём первую <picture>, а не последнюю"
    assert p0.in_stock is True

    # vendor.model-выгрузка: имени нет, собираем из vendor+model.
    assert p1.name == "Гростер Касалетка 100 мл", p1.name
    assert p1.price == 11.8, "запятая как десятичный разделитель"
    assert p1.in_stock is False, 'available="false" — не в наличии'
    assert p1.product_url == "", "нет <url> — пустая строка, а не падение"

    # YML топ-5 и подписчиков не несёт: секции пустые, import_all их не тронет.
    assert r["top5"] == [] and r["subscribers"] == []

    # Кривой файл → понятная ValueError, а не краш и не частичный импорт.
    bad = [
        (b"", "пустой"),
        (b"<yml_catalog>", "битый XML"),
        (b"<grosterhit-import/>", "чужой корень"),
        (b"<yml_catalog/>", "без <shop>"),
        (b"<yml_catalog><shop><offers><offer><price>1</price></offer></offers></shop></yml_catalog>",
         "offer без id"),
        (b"<yml_catalog><shop><offers><offer id='1'><price>1</price></offer></offers></shop></yml_catalog>",
         "offer без categoryId"),
        (b"<yml_catalog><shop><offers><offer id='1'><categoryId>c</categoryId>"
         b"<price>71,60 RUB</price></offer></offers></shop></yml_catalog>", "цена не число"),
        (b"<yml_catalog><shop><offers/></shop></yml_catalog>", "пустой каталог"),
    ]
    for data, why in bad:
        try:
            parse(data)
            assert False, f"должно упасть: {why}"
        except ValueError:
            pass

    print("import_yml._demo OK")


if __name__ == "__main__":
    _demo()
