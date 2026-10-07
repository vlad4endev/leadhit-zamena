"""Фото каталога: свой резолвер расширения.

Выгрузка 1С проставляет всем картинкам `.png`, но на static.groster.me часть файлов
лежит как `.jpg`/`.jpeg` — такие ссылки отдают 404 (проверка 2026-08-15: из 1506
ссылок 1103 живут как .png, 330 только как .jpg, 20 только как .jpeg, 53 нет вовсе).
Расширение по URL не угадать — связи с артикулом и датой у него нет, поэтому в БД
кладём ссылку на свой `/img/<имя>`, а он при первом обращении спрашивает static о
реальном файле и редиректит на него. Импорт при этом не делает ни одного запроса:
цена вопроса — один HEAD на картинку за время жизни процесса.

Если файла нет ни в одном расширении — 404 и image_url в БД обнуляется: иначе в топ-5
висит «нет фото» при зелёном «в наличии» (ссылка в БД есть, на CDN — нет).

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

# ponytail: кэш в памяти процесса, живёт до рестарта. "" = проверили, файла нет.
# Хватает — картинок ~2к, промах стоит один HEAD. Понадобится общий на воркеры — app_config.
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


def has_photo(url: str | None) -> bool:
    """Есть GUID-фото: непустой image_url (после proxied пусто = артикул/нет файла)."""
    return bool(url and str(url).strip())


# SQL-фрагмент: товар годится в авторассылку (есть фото). Подставлять в WHERE как есть.
HAS_PHOTO_SQL = "image_url IS NOT NULL AND btrim(image_url) <> ''"


def photo_first(products: list[dict], limit: int) -> list[dict]:
    """Товары для письма: только с фото, порядок подборки сохраняется, не больше limit.

    Без GUID-ссылки в авторассылку не берём — плейсхолдер «нет фото» в письме хуже,
    чем меньшая подборка. Такие товары смотрят во вкладке «Без фото» админки.
    """
    out = [p for p in products if has_photo(p.get("image_url"))]
    return out[:limit]


def _head_ok(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _resolve_sync(name: str) -> str | None:
    """Имя из выгрузки → URL файла на static, либо None если файла нет нигде.

    Сначала расширение как прислали, потом остальные. None — честный «фото нет»,
    а не 302 на заведомый 404 (иначе в топ-5 карточка с «нет фото» и статусом «в наличии»).
    """
    base, _, ext = name.rpartition(".")
    for e in ("." + ext, *(x for x in EXTS if x != "." + ext)):
        if _head_ok(PREFIX + base + e):
            return PREFIX + base + e
    return None


def url_alive_sync(url: str) -> bool:
    """Жива ли ссылка на фото (для чистки топ-5). /img/… — через резолвер расширений."""
    if not url:
        return False
    if url.startswith("/img/"):
        name = url[len("/img/"):]
        if not _NAME_RE.match(name):
            return False
        cached = _resolved.get(name)
        if cached is not None:
            return cached != ""
        found = _resolve_sync(name)
        _resolved[name] = found or ""
        return found is not None
    if url.startswith(PREFIX):
        name = url[len(PREFIX):]
        if _NAME_RE.match(name):
            return url_alive_sync("/img/" + name)
        return _head_ok(url)
    if url.startswith("http://") or url.startswith("https://"):
        return _head_ok(url)
    return False


async def resolve(name: str) -> str | None:
    """Асинхронная обёртка резолвера с кэшем процесса."""
    if not _NAME_RE.match(name):
        return None
    cached = _resolved.get(name)
    if cached is not None:
        return cached or None
    found = await asyncio.to_thread(_resolve_sync, name)
    _resolved[name] = found or ""
    return found


async def clear_broken_image_url(name: str) -> int:
    """Обнулить image_url у товаров с /img/<name> или абсолютным static…/<base>.*."""
    from app import db
    if not _NAME_RE.match(name):
        return 0
    base = name.rsplit(".", 1)[0]
    async with db.pool().acquire() as con:
        return int(await con.fetchval(
            """WITH u AS (
                 UPDATE products
                    SET image_url = NULL, updated_at = now()
                  WHERE image_url = $1
                     OR image_url = $2
                     OR image_url LIKE $3
                     OR image_url LIKE $4
                 RETURNING 1
               )
               SELECT count(*)::int FROM u""",
            "/img/" + name,
            PREFIX + name,
            "/img/" + base + ".%",
            PREFIX + base + ".%",
        ) or 0)


@router.get("/img/{name}")
@router.head("/img/{name}")
async def image(name: str):
    """302 на реальный файл. Нет файла — 404 и чистим битую ссылку в products."""
    if not _NAME_RE.match(name):
        raise HTTPException(404, "нет такой картинки")
    url = await resolve(name)
    if not url:
        # ponytail: побочный эффект на GET картинки — иначе топ-5 вечно показывает
        # «нет фото» при непустом image_url. Один UPDATE на промах, дальше кэш "".
        try:
            await clear_broken_image_url(name)
        except Exception:  # noqa: BLE001 — отдача 404 важнее учёта в БД
            pass
        raise HTTPException(404, "нет такой картинки")
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

    # Только с фото, порядок подборки сохраняется, без фото выкидываются.
    assert has_photo("/img/x.png") and not has_photo(None) and not has_photo("")
    pp = [{"product_id": "a"}, {"product_id": "b", "image_url": "/img/b.png"},
          {"product_id": "c", "image_url": ""}, {"product_id": "d", "image_url": "/img/d.png"}]
    assert [x["product_id"] for x in photo_first(pp, 10)] == ["b", "d"]
    assert [x["product_id"] for x in photo_first(pp, 1)] == ["b"]
    assert photo_first([], 5) == []

    global _head_ok, _resolved
    real, alive = _head_ok, set()
    _resolved.clear()
    # Имя ≥4 символов — как в _NAME_RE (короткое «x.png» резолвер отклонит).
    n = "abcd.png"
    try:
        _head_ok = lambda u: u in alive  # noqa: E731
        # Файл лежит как .jpg, хотя выгрузка прислала .png.
        alive = {PREFIX + "abcd.jpg"}
        assert _resolve_sync(n) == PREFIX + "abcd.jpg"
        # Прислали верное расширение — берём его с первой попытки.
        alive = {PREFIX + "abcd.png", PREFIX + "abcd.jpg"}
        assert _resolve_sync(n) == PREFIX + "abcd.png"
        # Нет нигде — None (раньше возвращали битый URL → «нет фото» в топ-5).
        alive = set()
        assert _resolve_sync(n) is None
        assert url_alive_sync("/img/" + n) is False
        alive = {PREFIX + "abcd.jpg"}
        _resolved.clear()
        assert url_alive_sync("/img/" + n) is True
    finally:
        _head_ok = real
        _resolved.clear()
    print("images._demo OK")


if __name__ == "__main__":
    _demo()
