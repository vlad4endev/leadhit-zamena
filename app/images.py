"""Фото каталога: резолвер расширения + прогрев перед UI/рассылкой.

Выгрузка 1С всем ставит `.png`, на static часть файлов — `.jpg`/`.jpeg`. В БД кладём
`/img/<имя>`; при запросе/прогреве спрашиваем CDN и получаем живой URL.

Важно: отрицательный кэш и обнуление image_url — только при твёрдом 404 по всем
расширениям. Таймаут/5xx/обрыв HEAD не считаем «фото нет» (раньше из‑за этого
пропадали рабочие картинки). В админке и письмах отдаём уже абсолютный CDN-URL,
чтобы не зависеть от редиректа /img/ в почтовике.
"""
from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["images"])

PREFIX = "https://static.groster.me/images/shop/"
EXTS = (".png", ".jpg", ".jpeg")
_NAME_RE = re.compile(r"^[0-9A-Za-z_-]{4,64}\.(png|jpe?g)$")

# Кэш процесса: url = живой CDN; "" = твёрдый промах (404 по всем расширениям).
# Мягкий сбой (таймаут/5xx) в кэш как промах НЕ пишем.
_resolved: dict[str, str] = {}


@dataclass(frozen=True)
class Probe:
    ok: bool
    hard_miss: bool  # явный 404/410 — можно считать файла нет


def _built_from_product_id(url: str, product_id: str) -> bool:
    """Ссылка из артикула: имя файла = product_id + расширение. Таких файлов на CDN нет."""
    name = url.split("?")[0].split("#")[0].rsplit("/", 1)[-1].lower()
    return name in {product_id.lower() + e for e in EXTS}


def proxied(url: str | None, product_id: str | None = None) -> str | None:
    """URL из выгрузки → /img/<имя> для static.groster.me; артикульные — None."""
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
    return bool(url and str(url).strip())


HAS_PHOTO_SQL = "image_url IS NOT NULL AND btrim(image_url) <> ''"


def photo_first(products: list[dict], limit: int) -> list[dict]:
    """Только с непустым image_url; порядок подборки сохраняется."""
    return [p for p in products if has_photo(p.get("image_url"))][:limit]


def _probe_sync(url: str) -> Probe:
    """Проверка файла на CDN. HEAD, при неудаче — GET Range (многие CDN режут HEAD)."""
    for method, headers in (
        ("HEAD", {}),
        ("GET", {"Range": "bytes=0-0"}),
    ):
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                if r.status in (200, 206):
                    return Probe(ok=True, hard_miss=False)
                if r.status in (404, 410):
                    return Probe(ok=False, hard_miss=True)
                return Probe(ok=False, hard_miss=False)
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return Probe(ok=False, hard_miss=True)
            # 405 на HEAD → пробуем GET; прочие коды — мягкий промах
            if method == "HEAD" and e.code in (405, 403, 401):
                continue
            return Probe(ok=False, hard_miss=False)
        except (urllib.error.URLError, OSError, TimeoutError):
            if method == "HEAD":
                continue
            return Probe(ok=False, hard_miss=False)
    return Probe(ok=False, hard_miss=False)


# Тесты подменяют _probe_sync через этот алиас старого имени.
def _head_ok(url: str) -> bool:
    return _probe_sync(url).ok


def _resolve_sync(name: str) -> str | None:
    """Имя → живой CDN URL, либо None.

    Твёрдый промах (все расширения 404) кэшируется как "".
    Мягкий сбой — None без записи в кэш (повторим при следующем прогреве).
    """
    if name in _resolved:
        return _resolved[name] or None
    base, _, ext = name.rpartition(".")
    soft = False
    for e in ("." + ext, *(x for x in EXTS if x != "." + ext)):
        p = _probe_sync(PREFIX + base + e)
        if p.ok:
            _resolved[name] = PREFIX + base + e
            return _resolved[name]
        if not p.hard_miss:
            soft = True
    if soft:
        return None  # не кэшируем промах — сеть могла моргнуть
    _resolved[name] = ""
    return None


def live_url_sync(url: str | None) -> str | None:
    """image_url из БД → абсолютный живой URL для <img>/письма, либо None."""
    if not url:
        return None
    if url.startswith("/img/"):
        name = url[len("/img/"):]
        if not _NAME_RE.match(name):
            return None
        return _resolve_sync(name)
    if url.startswith(PREFIX):
        name = url[len(PREFIX):]
        if _NAME_RE.match(name):
            return _resolve_sync(name)
        return url if _probe_sync(url).ok else None
    if url.startswith("http://") or url.startswith("https://"):
        p = _probe_sync(url)
        return url if p.ok else None
    return None


async def live_url(url: str | None) -> str | None:
    return await asyncio.to_thread(live_url_sync, url)


async def warm_products(products: list[dict], *, require_live: bool = True) -> list[dict]:
    """Прогрев фото: подставить в image_url абсолютный CDN-URL.

    require_live=True (письма) — без живого фото товар выкидывается.
    require_live=False (админка) — оставляем исходный url, если прогрев не удался мягко.
    """
    if not products:
        return []

    async def one(p: dict) -> dict | None:
        src = p.get("image_url")
        live = await live_url(src)
        q = dict(p)
        if live:
            q["image_url"] = live
            return q
        if require_live:
            return None
        # Мягкий сбой: не трогаем url (браузер ещё может открыть /img/ сам).
        return q if has_photo(src) else None

    # Параллельно, но с потолком — не долбим CDN сотнями соединений.
    sem = asyncio.Semaphore(12)

    async def guarded(p: dict) -> dict | None:
        async with sem:
            return await one(p)

    warmed = await asyncio.gather(*(guarded(p) for p in products))
    return [p for p in warmed if p is not None]


async def resolve(name: str) -> str | None:
    if not _NAME_RE.match(name):
        return None
    return await asyncio.to_thread(_resolve_sync, name)


@router.get("/img/{name}")
@router.head("/img/{name}")
async def image(name: str):
    """302 на живой файл. Твёрдый промах — 404 (БД тут не трогаем: обнуление только
    после явной проверки в прогреве/письме, чтобы таймаут не сжёг рабочие ссылки)."""
    if not _NAME_RE.match(name):
        raise HTTPException(404, "нет такой картинки")
    url = await resolve(name)
    if not url:
        raise HTTPException(404, "нет такой картинки")
    return RedirectResponse(url, status_code=302)


def _demo() -> None:
    assert proxied(PREFIX + "0140267.png") == "/img/0140267.png"
    assert proxied(PREFIX + "1f64c996-3d86-11ed-948a-ac1f6b855a52.png") \
        == "/img/1f64c996-3d86-11ed-948a-ac1f6b855a52.png"
    assert proxied("https://groster.me/x.jpg") == "https://groster.me/x.jpg"
    assert proxied("") == "" and proxied(None) is None
    assert proxied(PREFIX + "../../etc/passwd") == PREFIX + "../../etc/passwd"
    assert proxied(PREFIX + "a/b.png") == PREFIX + "a/b.png"
    assert proxied(PREFIX + "0126367.png", "0126367") is None
    assert proxied(PREFIX + "0126367.jpg", "0126367") is None
    assert proxied("https://groster.me/upload/iblock/0057412.jpg", "0057412") is None
    assert proxied("https://groster.me/upload/iblock/0057412.JPG?v=2", "0057412") is None
    assert proxied("https://groster.me/upload/iblock/0057412.jpg") \
        == "https://groster.me/upload/iblock/0057412.jpg"
    assert proxied("https://groster.me/upload/iblock/0057412-2.jpg", "0057412") \
        == "https://groster.me/upload/iblock/0057412-2.jpg"
    assert proxied(PREFIX + "6aecaf12-c045-11ee-8805-ac1f6b855a52.png", "0126367") \
        == "/img/6aecaf12-c045-11ee-8805-ac1f6b855a52.png"

    assert has_photo("/img/x.png") and not has_photo(None) and not has_photo("")
    pp = [{"product_id": "a"}, {"product_id": "b", "image_url": "/img/b.png"},
          {"product_id": "c", "image_url": ""}, {"product_id": "d", "image_url": "/img/d.png"}]
    assert [x["product_id"] for x in photo_first(pp, 10)] == ["b", "d"]
    assert [x["product_id"] for x in photo_first(pp, 1)] == ["b"]
    assert photo_first([], 5) == []

    global _probe_sync, _resolved
    real = _probe_sync
    _resolved.clear()
    n = "abcd.png"
    # status: ok / hard / soft
    state: dict[str, str] = {}

    def fake(url: str) -> Probe:
        s = state.get(url, "hard")
        if s == "ok":
            return Probe(True, False)
        if s == "soft":
            return Probe(False, False)
        return Probe(False, True)

    try:
        _probe_sync = fake  # type: ignore[assignment]
        state = {PREFIX + "abcd.jpg": "ok"}
        assert _resolve_sync(n) == PREFIX + "abcd.jpg"
        _resolved.clear()
        state = {PREFIX + "abcd.png": "ok", PREFIX + "abcd.jpg": "ok"}
        assert _resolve_sync(n) == PREFIX + "abcd.png"
        _resolved.clear()
        state = {}
        assert _resolve_sync(n) is None and _resolved.get(n) == ""  # твёрдый промах в кэше
        _resolved.clear()
        state = {PREFIX + "abcd.png": "soft", PREFIX + "abcd.jpg": "soft",
                 PREFIX + "abcd.jpeg": "soft"}
        assert _resolve_sync(n) is None and n not in _resolved  # мягкий — без кэша
        assert live_url_sync("/img/" + n) is None
        _resolved.clear()
        state = {PREFIX + "abcd.jpg": "ok"}
        assert live_url_sync("/img/" + n) == PREFIX + "abcd.jpg"
    finally:
        _probe_sync = real  # type: ignore[assignment]
        _resolved.clear()
    print("images._demo OK")


if __name__ == "__main__":
    _demo()
