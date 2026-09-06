"""Фото каталога: свой резолвер расширения.

Выгрузка 1С проставляет всем картинкам `.png`, но на static.groster.me часть файлов
лежит как `.jpg`/`.jpeg` — такие ссылки отдают 404 (проверка 2026-08-15: из 1506
ссылок 1103 живут как .png, 330 только как .jpg, 20 только как .jpeg, 53 нет вовсе).
Расширение по URL не угадать — связи с артикулом и датой у него нет, поэтому в БД
кладём ссылку на свой `/img/<имя>`, а он при первом обращении спрашивает static о
реальном файле и редиректит на него. Импорт при этом не делает ни одного запроса:
цена вопроса — один HEAD на картинку за время жизни процесса.

Когда выгрузку починят (см. db/import_json_contract.md), ничего убирать не нужно:
верное расширение резолвер угадает с первой попытки.
"""
from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["images"])

PREFIX = "https://static.groster.me/images/shop/"
EXTS = (".png", ".jpg", ".jpeg")
# Имя файла: GUID картинки или артикул. Ни слэшей, ни точек кроме расширения —
# в CDN-URL подставляется только то, что прошло эту проверку.
_NAME_RE = re.compile(r"^[0-9A-Za-z_-]{4,64}\.(png|jpe?g)$")

# ponytail: кэш в памяти процесса, живёт до рестарта. Хватает — картинок ~2к,
# промах стоит один HEAD. Понадобится общий на воркеры — класть в app_config.
_resolved: dict[str, str] = {}


def _built_from_product_id(url: str, product_id: str) -> bool:
    """Ссылка собрана из артикула: имя файла = product_id + расширение картинки.

    Проверка на ЛЮБОМ хосте, а не только на static: 1С-контракт долго показывал пример
    `https://groster.me/upload/iblock/<артикул>.jpg`, и выгрузки его повторяли — таких
    файлов нет ни на одном хосте (проверка 2026-09-06: 12 из 12 → 404 на groster.me,
    ранее 60 из 60 на static). Имя картинки — GUID из CMS, из артикула не выводится.
    """
    name = url.split("?")[0].split("#")[0].rsplit("/", 1)[-1].lower()
    return name in {product_id.lower() + e for e in EXTS}


def proxied(url: str | None, product_id: str | None = None) -> str | None:
    """URL картинки из выгрузки → относительная ссылка на резолвер.

    Чужие хосты, пустые значения и уже проксированное — возвращаем как есть.

    Ссылку, собранную из артикула, считаем отсутствующей и отдаём None: файла по ней
    нет, а битую ссылку хранить вреднее, чем пустоту — она затирает рабочую ссылку того
    же товара из другой выгрузки (см. COALESCE в app/feeds.py) и уезжает в письмо
    сломанной картинкой. Товар покажет плейсхолдер «нет фото», пока не придёт GUID-URL.
    """
    if not url:
        return url
    if product_id and _built_from_product_id(url, product_id):
        return None
    if not url.startswith(PREFIX):
        return url
    name = url[len(PREFIX):]
    if not _NAME_RE.match(name):
        return url
    return "/img/" + name


def photo_first(products: list[dict], limit: int) -> list[dict]:
    """Товары для письма: сначала с фото, потом остальные, всего не больше limit.

    Сортировка устойчивая — внутри групп сохраняется порядок подборки (позиции из
    админки/фида). Так карточки-плейсхолдеры «нет фото» уходят в хвост и обрезаются
    первыми: пока выгрузка 1С не отдаёт GUID-ссылки, без фото сидит заметная часть
    каталога (см. проверку в шапке модуля), и письмо из одних плейсхолдеров — худшее,
    что можно показать. Тот же порядок у превью шаблона в админке.
    """
    return sorted(products, key=lambda p: not p.get("image_url"))[:limit]


def _head_ok(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _resolve_sync(name: str) -> str:
    """Имя из выгрузки → URL файла, который на static реально есть.

    Сначала пробуем расширение как прислали, потом остальные. Не нашли ничего —
    отдаём исходное: пусть будет честный 404, а не молчаливая подмена.
    """
    base, _, ext = name.rpartition(".")
    for e in ("." + ext, *(x for x in EXTS if x != "." + ext)):
        if _head_ok(PREFIX + base + e):
            return PREFIX + base + e
    return PREFIX + name


@router.get("/img/{name}")
async def image(name: str):
    """302 на реальный файл картинки. 302, а не 301: ошибочный ответ не должен
    залипать в кэше почтовика навсегда."""
    if not _NAME_RE.match(name):
        raise HTTPException(404, "нет такой картинки")
    url = _resolved.get(name)
    if url is None:
        url = _resolved[name] = await asyncio.to_thread(_resolve_sync, name)
    return RedirectResponse(url, status_code=302)


def _demo() -> None:
    """Self-check без сети и БД: подменяем HEAD, проверяем обе чистые функции."""
    assert proxied(PREFIX + "0140267.png") == "/img/0140267.png"
    assert proxied(PREFIX + "1f64c996-3d86-11ed-948a-ac1f6b855a52.png") \
        == "/img/1f64c996-3d86-11ed-948a-ac1f6b855a52.png"
    # Чужое, пустое и подозрительное — не трогаем.
    assert proxied("https://groster.me/x.jpg") == "https://groster.me/x.jpg"
    assert proxied("") == "" and proxied(None) is None
    assert proxied(PREFIX + "../../etc/passwd") == PREFIX + "../../etc/passwd"
    assert proxied(PREFIX + "a/b.png") == PREFIX + "a/b.png"
    # Ссылка из артикула («сопутствующие») — считаем, что фото нет. Хост любой:
    # тот же мусор приходит с groster.me/upload/iblock (пример из 1С-контракта).
    assert proxied(PREFIX + "0126367.png", "0126367") is None
    assert proxied(PREFIX + "0126367.jpg", "0126367") is None
    assert proxied("https://groster.me/upload/iblock/0057412.jpg", "0057412") is None
    assert proxied("https://groster.me/upload/iblock/0057412.JPG?v=2", "0057412") is None
    # Без product_id (фид без артикула в этом месте) — ссылку не трогаем.
    assert proxied("https://groster.me/upload/iblock/0057412.jpg") \
        == "https://groster.me/upload/iblock/0057412.jpg"
    # Артикул — часть имени, но не всё имя: это нормальная ссылка.
    assert proxied("https://groster.me/upload/iblock/0057412-2.jpg", "0057412") \
        == "https://groster.me/upload/iblock/0057412-2.jpg"
    # Тот же артикул, но имя — GUID: нормальная ссылка, не трогаем.
    assert proxied(PREFIX + "6aecaf12-c045-11ee-8805-ac1f6b855a52.png", "0126367") \
        == "/img/6aecaf12-c045-11ee-8805-ac1f6b855a52.png"

    # Фото вперёд, порядок подборки внутри групп сохраняется, лишнее обрезается.
    pp = [{"product_id": "a"}, {"product_id": "b", "image_url": "/img/b.png"},
          {"product_id": "c", "image_url": ""}, {"product_id": "d", "image_url": "/img/d.png"}]
    assert [x["product_id"] for x in photo_first(pp, 10)] == ["b", "d", "a", "c"]
    assert [x["product_id"] for x in photo_first(pp, 2)] == ["b", "d"]   # без фото обрезаются первыми
    assert photo_first([], 5) == []

    global _head_ok
    real, alive = _head_ok, set()
    try:
        _head_ok = lambda u: u in alive  # noqa: E731
        # Файл лежит как .jpg, хотя выгрузка прислала .png.
        alive = {PREFIX + "x.jpg"}
        assert _resolve_sync("x.png") == PREFIX + "x.jpg"
        # Прислали верное расширение — берём его с первой попытки.
        alive = {PREFIX + "x.png", PREFIX + "x.jpg"}
        assert _resolve_sync("x.png") == PREFIX + "x.png"
        # Нет нигде — отдаём как прислали.
        alive = set()
        assert _resolve_sync("x.png") == PREFIX + "x.png"
    finally:
        _head_ok = real
    print("images._demo OK")


if __name__ == "__main__":
    _demo()
