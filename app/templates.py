"""Рендер email-шаблонов. Два макета (default/minimal) + настраиваемое оформление (look).

look: {brand_color, header, button, footer} — редактируется в разделе «Шаблоны писем».
Выбор макета задаётся в настройках сценария (cfg['template']).
"""
from __future__ import annotations

import html as _htmllib
import re
from html.parser import HTMLParser

from app.config import settings

# Публичный адрес сервиса — из настроек (админка → «Интеграция», поверх .env), а не
# константой на импорте: адрес меняется в рантайме, ссылка отписки должна вести туда,
# где реально отвечает /unsubscribe.
def unsub_base() -> str:
    from app import app_settings
    return app_settings.public_base_url() + "/unsubscribe"


def img_src(url: str | None) -> str:
    """Ссылка на фото для письма. После warm_products сюда уже приходит абсолютный
    CDN-URL; /img/… — запасной путь (публичный хост сервиса → редирект)."""
    if not url:
        return ""
    if url.startswith("/"):
        from app import app_settings
        return app_settings.public_base_url().rstrip("/") + url
    return url

# Разрешённый набор тегов для rich-text (текст/колонки). Всё остальное вырезается.
_RT_TAGS = {"b", "strong", "i", "em", "u", "s", "a", "br", "p", "ul", "ol", "li", "h3", "h4", "span"}


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in _RT_TAGS:
            return
        if tag == "a":
            href = dict(attrs).get("href", "") or ""
            if href.lower().startswith(("http://", "https://", "mailto:")):
                self.out.append(f'<a href="{_htmllib.escape(href, quote=True)}" target="_blank" rel="noopener">')
            else:
                self.out.append("<a>")
        else:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in _RT_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        self.out.append(_htmllib.escape(data))


def sanitize_html(s: str) -> str:
    """Оставляет только безопасный набор inline-тегов (для rich-text из contenteditable)."""
    p = _Sanitizer()
    p.feed(s or "")
    return "".join(p.out)

LOOK_DEFAULTS = {
    "brand_color": "#a81fcb",       # фирменная маджента Groster
    "header": "Groster.me",         # шапка/логотип-текст
    "button": "Купить",             # текст кнопки товара
    "footer": "Отписаться от рассылки",
}


def _look(look: dict | None) -> dict:
    return {**LOOK_DEFAULTS, **(look or {})}


def _esc(s) -> str:
    return _htmllib.escape(str(s if s is not None else ""))


_ALIGNS = {"left", "center", "right"}
_HEAD_SIZE = {"s": "17px", "m": "22px", "l": "28px"}
_TEXT_SIZE = {"s": "13px", "m": "15px", "l": "18px"}
_LH = {"s": "1.3", "m": "1.55", "l": "1.9"}
_SPACE = {"s": "6px", "m": "16px", "l": "30px"}
_RADIUS = {"none": "0", "s": "10px", "l": "20px"}
_FONTS = {
    "serif": "Georgia, 'Times New Roman', serif",
    "mono": "'Courier New', Courier, monospace",
    "rounded": "'Trebuchet MS', Verdana, sans-serif",
}


def _align(a) -> str:
    return a if a in _ALIGNS else "left"


def _font_css(key) -> str:
    f = _FONTS.get(key)
    return f";font-family:{f}" if f else ""


def _valid_color(c) -> bool:
    return (isinstance(c, str) and c.startswith("#") and 4 <= len(c) <= 9
            and all(ch in "0123456789abcdefABCDEF" for ch in c[1:]))


def _color(c, default: str) -> str:
    return c if _valid_color(c) else default


def _utm(url: str, campaign: str) -> str:
    # Пустой url → "#": иначе получалось href="?utm_source=…" (ссылка на сам файл письма).
    if not (url or "").strip():
        return "#"
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}utm_source=trigger&utm_campaign={campaign}"


def _price(v) -> str:
    """Цена как в админке (priceRu): 71.6 → «71,60 ₽», 12 → «12 ₽».
    int() округлял вниз и терял копейки — в письме была цена «71 ₽» вместо 71,60.
    Разряды и пробел перед ₽ — неразрывные (U+00A0): цена не переносится по строкам."""
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        return ""
    s = f"{n:,.2f}" if n % 1 else f"{n:,.0f}"
    return s.replace(",", "\u00a0").replace(".", ",") + "\u00a0₽"


CARDS_PER_ROW = 3  # 3×180px ≈ 600px влезает в 640px письма; больше — карточки уезжают за край
CARD_IMG = 150     # квадрат под фото — иначе разные пропорции рвут ряд
TITLE_CHARS = 52   # ~2–3 строки в колонке 160px; длинные названия ломали выравнивание кнопок
TITLE_H = 52       # фиксированная высота блока названия (3 × ~17px)


def _short_name(name: str, limit: int = TITLE_CHARS) -> str:
    """Обрезает название по слову, чтобы карточки в ряду были одной высоты."""
    n = " ".join((name or "").split())
    if len(n) <= limit:
        return n
    cut = n[: limit - 1].rsplit(" ", 1)[0]
    return (cut or n[: limit - 1]).rstrip(".,;:") + "…"


def _card(p: dict, campaign: str, minimal: bool, lk: dict) -> str:
    url = _esc(_utm(p.get("product_url"), campaign))
    full_name = p.get("name") or ""
    name = _esc(_short_name(full_name))
    alt = _esc(full_name)  # полный alt — доступность / если фото 404
    price = _price(p.get("price"))
    if minimal:
        return f'<tr><td style="padding:6px 0"><a href="{url}">{name}</a> — {price}</td></tr>'
    # Квадрат CARD_IMG×CARD_IMG: разные пропорции фото больше не сдвигают цену/кнопку.
    # object-fit:contain — современные клиенты; Outlook оставит max-height в ячейке фиксированной высоты.
    if p.get("image_url"):
        img = (f'<img src="{_esc(img_src(p["image_url"]))}" width="{CARD_IMG}" alt="{alt}" '
               f'style="display:block;margin:0 auto;max-width:{CARD_IMG}px;max-height:{CARD_IMG}px;'
               f'width:auto;height:auto;object-fit:contain;border:0;border-radius:8px">')
    else:
        img = (f'<div style="width:{CARD_IMG}px;height:{CARD_IMG}px;margin:0 auto;'
               f'background:#eef2f8;border-radius:8px"></div>')
    btn = (f'<a href="{url}" style="display:inline-block;background:{lk["brand_color"]};color:#fff;'
           f'text-decoration:none;padding:8px 16px;border-radius:8px;font-size:14px;'
           f'font-weight:600;line-height:1.2">{lk["button"]}</a>')
    # Вложенная таблица: фото → название (фикс. высота) → цена → кнопка — кнопки в ряду на одной линии.
    return (
        f'<td width="180" valign="top" style="padding:8px 6px;text-align:center">'
        f'<table role="presentation" width="168" cellpadding="0" cellspacing="0" '
        f'style="margin:0 auto;border-collapse:collapse">'
        f'<tr><td align="center" valign="middle" height="{CARD_IMG}" '
        f'style="height:{CARD_IMG}px;width:{CARD_IMG}px;background:#fafafa;border-radius:8px;'
        f'vertical-align:middle">{img}</td></tr>'
        f'<tr><td align="center" valign="top" height="{TITLE_H}" '
        f'style="height:{TITLE_H}px;max-height:{TITLE_H}px;padding:10px 4px 4px;font-weight:600;'
        f'font-size:13px;line-height:1.35;color:#1a1a2e;overflow:hidden;vertical-align:top">'
        f'{name}</td></tr>'
        f'<tr><td align="center" style="padding:2px 0 10px;color:#555;font-size:14px;'
        f'line-height:1.2;white-space:nowrap">{price}</td></tr>'
        f'<tr><td align="center" style="padding:0 0 4px">{btn}</td></tr>'
        f'</table></td>'
    )


def _cards_table(products: list[dict], campaign: str, lk: dict) -> str:
    """Сетка карточек по CARDS_PER_ROW в строке. Одной строкой 5–8 товаров вылезали
    за границу письма (в почтовых клиентах нет горизонтального скролла).
    Пустые ячейки добивают ряд до CARDS_PER_ROW — иначе последняя строка «плывёт» влево."""
    cells = [_card(p, campaign, False, lk) for p in products]
    while cells and len(cells) % CARDS_PER_ROW:
        cells.append('<td width="180" style="padding:8px 6px">&nbsp;</td>')
    rows = "".join(
        f'<tr>{"".join(cells[i:i + CARDS_PER_ROW])}</tr>'
        for i in range(0, len(cells), CARDS_PER_ROW))
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:8px auto;border-collapse:collapse;max-width:560px">{rows}</table>')


def render_email(intro_html: str, products: list[dict], user_id: str,
                 campaign: str, template: str = "default", look: dict | None = None) -> str:
    lk = _look(look)
    unsub = f'{unsub_base()}?u={user_id}&c={campaign}'
    minimal = template == "minimal"

    if minimal:
        rows = "".join(_card(p, campaign, True, lk) for p in products)
        return (
            f'<div style="font-family:sans-serif;max-width:600px;color:#222">'
            f'{intro_html}<table style="width:100%">{rows}</table>'
            f'<p style="font-size:12px;color:#888;margin-top:16px">'
            f'<a href="{unsub}">{lk["footer"]}</a></p></div>'
        )

    return (
        f'<div style="font-family:sans-serif;max-width:640px;margin:0 auto;'
        f'border:1px solid #e6ebf3;border-radius:14px;overflow:hidden">'
        f'<div style="background:{lk["brand_color"]};color:#fff;padding:16px 24px;font-weight:700;font-size:18px">'
        f'{lk["header"]}</div>'
        f'<div style="padding:24px">{intro_html}'
        f'{_cards_table(products, campaign, lk)}</div>'
        f'<div style="background:#f5f7fb;padding:16px 24px;font-size:12px;color:#888">'
        f'<a href="{unsub}" style="color:#888">{lk["footer"]}</a></div></div>'
    )


# ── Конструктор шаблонов: письмо собирается из блоков (per-scenario) ──────────
# blocks: list[dict], каждый {"type": ..., ...props}. Порядок = порядок в письме.

def _cta(text: str, url: str, lk: dict, campaign: str) -> str:
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:14px auto"><tr>'
        f'<td style="background:{lk["brand_color"]};border-radius:8px">'
        f'<a href="{_utm(url or "#", campaign)}" style="display:inline-block;padding:12px 30px;'
        f'color:#fff;text-decoration:none;font-weight:600;font-size:15px">{_esc(text)}</a></td></tr></table>'
    )


def _render_block(b: dict, products: list[dict], campaign: str, lk: dict, user_id: str = "") -> str:
    t = b.get("type")
    if t == "heading":
        size = _HEAD_SIZE.get(b.get("size", "m"), _HEAD_SIZE["m"])
        style = (f'margin:16px 0 10px;font-size:{size};line-height:1.25;'
                 f'text-align:{_align(b.get("align"))};color:{_color(b.get("color"), "#1a1a2e")}'
                 f'{_font_css(b.get("font"))}')
        return f'<h2 style="{style}">{_esc(b.get("text"))}</h2>'
    if t == "text":
        # rich-text: приходит HTML из редактора, отдаём санитизированным (fallback — старый plain с \n)
        raw = b.get("html") or ""
        body = sanitize_html(raw) if "<" in raw else _esc(raw).replace("\n", "<br>")
        size = _TEXT_SIZE.get(b.get("size", "m"), _TEXT_SIZE["m"])
        lh = _LH.get(b.get("lh", "m"), _LH["m"])
        style = (f'margin:8px 0;line-height:{lh};font-size:{size};'
                 f'text-align:{_align(b.get("align"))};color:{_color(b.get("color"), "#333333")}'
                 f'{_font_css(b.get("font"))}')
        return f'<div style="{style}">{body}</div>'
    if t == "products":
        return _cards_table(products, campaign, lk)
    if t == "button":
        return f'<div style="text-align:{_align(b.get("align", "center"))}">{_cta(b.get("text", "Перейти"), b.get("url", "#"), lk, campaign)}</div>'
    if t == "image":
        if not b.get("src"):
            return ""
        rad = _RADIUS.get(b.get("radius", "s"), "10px")
        tag = (f'<img src="{_esc(b["src"])}" alt="{_esc(b.get("alt"))}" '
               f'style="max-width:100%;border-radius:{rad};display:block;margin:10px 0">')
        return f'<a href="{_utm(b["url"], campaign)}">{tag}</a>' if b.get("url") else tag
    if t == "divider":
        return '<hr style="border:0;border-top:1px solid #e6ebf3;margin:18px 0">'
    if t == "spacer":
        h = max(0, min(120, int(b.get("h", 16) or 0)))
        return f'<div style="height:{h}px;line-height:{h}px">&nbsp;</div>'
    if t == "quote":
        author = (f'<div style="margin-top:6px;font-size:13px;color:#888">— {_esc(b["author"])}</div>'
                  if b.get("author") else "")
        return (f'<blockquote style="border-left:3px solid {lk["brand_color"]};margin:16px 0;'
                f'padding:4px 0 4px 16px">'
                f'<div style="font-style:italic;color:#444;font-size:15px;line-height:1.5">{_esc(b.get("text"))}</div>'
                f'{author}</blockquote>')
    if t == "promo":
        caption = (f'<div style="font-size:13px;color:#666;margin-top:5px">{_esc(b["caption"])}</div>'
                   if b.get("caption") else "")
        return (f'<div style="text-align:center;margin:18px 0">'
                f'<div style="display:inline-block;border:2px dashed {lk["brand_color"]};border-radius:10px;padding:14px 30px">'
                f'<div style="font-size:23px;font-weight:800;letter-spacing:2px;font-family:monospace;color:{lk["brand_color"]}">'
                f'{_esc(b.get("code"))}</div>{caption}</div></div>')
    if t == "social":
        items = [("ВКонтакте", b.get("vk")), ("Telegram", b.get("telegram")),
                 ("WhatsApp", b.get("whatsapp")), ("Instagram", b.get("instagram"))]
        pills = "".join(
            f'<a href="{_utm(url, campaign)}" style="display:inline-block;margin:4px;padding:8px 15px;'
            f'background:#f0f2f7;border-radius:8px;color:{lk["brand_color"]};text-decoration:none;'
            f'font-weight:600;font-size:13px">{label}</a>'
            for label, url in items if url)
        return f'<div style="text-align:center;margin:14px 0">{pills}</div>' if pills else ""
    if t == "html":
        # Сырой HTML от админа (доверенный источник) — без санитизации: это единственный способ
        # сохранить табличную вёрстку готового письма. Но через Jinja-слой: в импортированных
        # письмах товары и фото приходят циклом {% for item in get_*() %} — без рендера
        # в письмо уезжал сам код шаблона вместо карточек.
        return render_html_template(b.get("html") or "", products, user_id, campaign, lk)
    if t == "columns":
        left = sanitize_html(b.get("left") or "")
        right = sanitize_html(b.get("right") or "")
        return (f'<table role="presentation" width="100%" style="margin:10px 0"><tr>'
                f'<td width="50%" valign="top" style="padding:0 10px 0 0;color:#333;font-size:14px;line-height:1.5">{left}</td>'
                f'<td width="50%" valign="top" style="padding:0 0 0 10px;color:#333;font-size:14px;line-height:1.5">{right}</td>'
                f'</tr></table>')
    return ""


class _Items(list):
    """Список товаров, который в Jinja работает и как get_recommendations(), и как
    get_recommendations без скобок: LeadHit-шаблоны пишут оба варианта, а обычный
    callable в {% for item in get_recommendations %} даёт TypeError → пустой блок."""

    def __call__(self, *args, **kwargs):
        return self


def _mjml_items(products: list[dict] | None, campaign: str) -> _Items:
    """Адаптер: наши товары → объекты, которых ждёт MJML-шаблон LeadHit.
    Шаблон обращается к item.url/picture/name/price и делит цену на 100 (LeadHit хранил
    копейки) — поэтому цену в рублях домножаем обратно. Дополнительно даём item.price_str —
    готовую цену «71,60 ₽» для своих шаблонов (в копейках/100 теряются копейки).
    url — с UTM, как у нативных карточек: иначе клики из импортированного шаблона не атрибутируются.
    image/image_url — те же фото, что picture: часть шаблонов ждёт другое имя поля."""
    out: _Items = _Items()
    for p in (products or []):
        picture = p.get("image_url") or ""
        picture_src = img_src(picture) if picture else ""
        out.append({
            "id": p.get("product_id") or "",
            "url": _utm(p.get("product_url") or "", campaign),
            "picture": picture_src,
            "image": picture_src,
            "image_url": picture_src,
            "name": p.get("name") or "",
            "price": int(round(float(p.get("price") or 0) * 100)),
            "price_str": _price(p.get("price")),
        })
    return out


_VOID_TAGS = {"img", "br", "hr", "meta", "input", "link", "area", "base", "col",
              "source", "wbr", "embed", "track", "param"}
_TOKEN_RE = re.compile(r'<!--.*?-->|<[^>]*>|[^<]+', re.S)


def _balance_html(s: str) -> str:
    """Чиним вёрстку, которую браузер прощает, а строгий mrml — нет: выкидываем сиротские
    закрывающие теги и дозакрываем незакрытые. Оригинальные байты валидных токенов сохраняем
    (переписываем только теги), чтобы не поломать аккуратную MJML-разметку."""
    out, stack = [], []
    for tok in _TOKEN_RE.findall(s):
        if not tok.startswith("<") or tok.startswith("<!"):
            out.append(tok)                                  # текст/комментарий/decl — как есть
            continue
        inner = tok[1:-1].strip()
        if inner.endswith("/") or not inner:                 # <x/> самозакрытый
            out.append(tok)
            continue
        if inner.startswith("/"):                            # закрывающий </x>
            name = inner[1:].strip().split()[0].lower() if inner[1:].strip() else ""
            if name in stack:
                while stack and stack[-1] != name:           # авто-закрыть вложенные
                    out.append(f"</{stack.pop()}>")
                stack.pop()
                out.append(tok)
            # иначе сиротский закрывающий — выкидываем
            continue
        name = inner.split()[0].lower()                      # открывающий <x ...>
        out.append(tok)
        if name and name not in _VOID_TAGS:
            stack.append(name)
    while stack:                                             # дозакрыть оставшееся
        out.append(f"</{stack.pop()}>")
    return "".join(out)


def _jinja_render(source: str, products: list[dict], user_id: str, campaign: str) -> str:
    """Прогон Jinja-слоя шаблона LeadHit (общий для MJML и HTML): подставляем товары/отписку,
    неизвестные переменные (lead.name, alert_name, …) → пусто, управляющие хелперы → no-op."""
    import datetime
    import jinja2
    unsub = f'{unsub_base()}?u={user_id}&c={campaign}'
    items = _mjml_items(products, campaign)
    # get_* — товары сценария. Ищем и get_foo(), и голое get_foo (без скобок).
    # _Items и итерируемый, и callable → оба синтаксиса {% for x in get_foo %} / get_foo().
    names = set(re.findall(r'(?<![\w.])(get_[A-Za-z0-9_]*)\s*\(', source))
    names.update(re.findall(r'(?<![\w.])(get_[A-Za-z0-9_]*)(?!\s*\()', source))
    ctx: dict = {
        "unsubscribe_url": unsub,
        # Алиасы без get_: часть экспортов LeadHit пишет {% for item in products %}.
        "products": items,
        "recommendations": items,
        "items": items,
        "get_recommendations": items,
        "get_cart_items": items,
    }
    for name in names:
        ctx[name] = items
    ctx["get_utc_time"] = lambda *a, **k: datetime.datetime.now(datetime.timezone.utc).isoformat()
    ctx["exit"] = ctx["abort"] = ctx["stop"] = lambda *a, **k: ""   # «не слать без данных» → no-op
    # ChainableUndefined: неизвестные переменные/атрибуты рендерятся пустыми, а не роняют шаблон.
    # autoescape=True: подставляем только текст/URL товаров, а имена вроде «Салфетка 30*30 "ГОРНИЦА"»
    # с кавычками и & иначе рвут атрибуты вёрстки (alt/src/href). На саму разметку шаблона
    # автоэкранирование не влияет — только на значения в {{ … }}.
    env = jinja2.Environment(autoescape=True, undefined=jinja2.ChainableUndefined)
    return env.from_string(source).render(**ctx)


_PRODUCT_HEADING_RE = re.compile(
    r"(Подборка товаров для вас|Подборка для вас|Вы забыли товары в корзине|"
    r"Возможно,\s*вам подойдёт|Вам подойдёт)",
    re.I,
)
_WHY_US_RE = re.compile(r"Почему выбирают", re.I)


def _html_already_has_products(html: str, products: list[dict]) -> bool:
    """В письме уже есть хотя бы одна карточка из переданной подборки?"""
    for p in products or []:
        name = (p.get("name") or "").strip()
        if name and _esc(name) in html:
            return True
        pic = img_src(p.get("image_url") or "")
        if pic and pic in html:
            return True
    return False


def _inject_products_if_missing(html: str, products: list[dict], campaign: str,
                                look: dict | None = None) -> str:
    """Если в готовом HTML есть заголовок подборки, но нет карточек (пустой цикл или
    «замороженный» после конвертации MJML шаблон) — вставляем сетку товаров."""
    if not html or not products:
        return html
    if _html_already_has_products(html, products):
        return html
    # Нет ни заголовка подборки, ни «Почему выбирают» — не наша зона, не трогаем вёрстку.
    if not _PRODUCT_HEADING_RE.search(html) and not _WHY_US_RE.search(html):
        # Импорт без привычных заголовков: если это письмо с get_* в исходнике уже
        # отработало в пустоту — всё равно вставим перед футером/концом body.
        if "unsubscribe" not in html.lower() and "</body>" not in html.lower():
            return html
    cards = (
        f'<div style="text-align:center;padding:8px 0">'
        f'{_cards_table(products, campaign, _look(look))}</div>'
    )
    # 1) Сразу после закрывающего тега ячейки/блока с заголовком подборки.
    m = _PRODUCT_HEADING_RE.search(html)
    if m:
        # Ищем конец текущего текстового узла + ближайший </div>|</td>|</p>
        tail = html[m.end(): m.end() + 400]
        close = re.search(r"</(?:div|td|p|h[1-6]|span)>", tail, re.I)
        if close:
            at = m.end() + close.end()
            return html[:at] + cards + html[at:]
        return html[: m.end()] + cards + html[m.end():]
    # 2) Перед блоком «Почему выбирают…»
    m2 = _WHY_US_RE.search(html)
    if m2:
        return html[: m2.start()] + cards + html[m2.start():]
    # 3) Перед </body> или в конец
    low = html.lower()
    bi = low.rfind("</body>")
    if bi != -1:
        return html[:bi] + cards + html[bi:]
    return html + cards


def render_html_template(raw: str, products: list[dict], user_id: str, campaign: str,
                         look: dict | None = None) -> str:
    """Готовый HTML-шаблон целиком. Если внутри есть Jinja ({{…}}/{%…%}) — прогоняем через
    тот же Jinja-слой (заполнит {{unsubscribe_url}}, циклы; неизвестное — пусто). Если Jinja
    не нужна или сломалась — отдаём как есть, подставив ссылку отписки.
    После рендера: если подборка пустая при живых товарах — вставляем карточки."""
    unsub = f'{unsub_base()}?u={user_id}&c={campaign}'
    if "{{" in raw or "{%" in raw:
        try:
            out = _jinja_render(raw, products, user_id, campaign)
            return _inject_products_if_missing(out, products, campaign, look)
        except Exception:  # noqa: BLE001 — не Jinja/битый шаблон → безопасный fallback
            pass
    out = raw.replace("{{unsubscribe_url}}", unsub)
    return _inject_products_if_missing(out, products, campaign, look)


def render_mjml(source: str, products: list[dict], user_id: str, campaign: str,
                look: dict | None = None) -> str:
    """Импортированный MJML-шаблон (Jinja + MJML): рендерим Jinja с товарами/отпиской,
    затем компилируем MJML → HTML. Ошибку показываем баннером (превью в админке видит проблему
    до активации; ponytail: без сложной обработки ошибок — админ проверяет письмо глазами)."""
    import mrml
    try:
        rendered = _jinja_render(source, products, user_id, campaign)
        # Отрезаем всё до <mjml> и после </mjml>: ведущие vars-комментарии, {%…%}, пробелы,
        # обёртка <html> — не должны ломать mrml (частая ошибка «position 0:5»).
        low = rendered.lower()
        i = low.find("<mjml")
        if i == -1:                             # это не MJML (нет <mjml>) — отдаём как HTML
            out = rendered.replace("{{unsubscribe_url}}", f'{unsub_base()}?u={user_id}&c={campaign}')
            return _inject_products_if_missing(out, products, campaign, look)
        j = low.rfind("</mjml>")
        mjml_str = rendered[i:(j + 7 if j != -1 else None)]
        try:
            res = mrml.to_html(mjml_str)
        except Exception:                       # битая вёрстка → чиним теги и пробуем ещё раз
            res = mrml.to_html(_balance_html(mjml_str))
        out = getattr(res, "content", res)
        return _inject_products_if_missing(out, products, campaign, look)
    except Exception as e:  # noqa: BLE001 — показываем причину в превью, не роняем воркер
        return (f'<div style="font-family:sans-serif;padding:24px;color:#b00020;line-height:1.5">'
                f'<b>Не удалось собрать MJML-шаблон.</b><br>'
                f'Скорее всего, ошибка в вёрстке исходника (незакрытые или лишние теги, '
                f'неподдерживаемый MJML-элемент). Откройте шаблон в mjml.io, исправьте вёрстку '
                f'и загрузите заново.<br><br><span style="color:#888;font-size:12px">Детали: '
                f'{_esc(type(e).__name__)}: {_esc(e)}</span></div>')


_JINJA_CTRL_RE = re.compile(
    r'\{%-?\s*(for|endfor|if|endif|elif|else|block|endblock|macro|endmacro|'
    r'set|endset|raw|endraw|call|endcall|filter|endfilter)\b',
    re.I,
)


def _jinja_ctrl_balance(html: str) -> int:
    """Грубый баланс управляющих тегов Jinja: >0 значит {% for/if %} без пары.
    Нужен, чтобы склеить html-блоки, когда цикл товаров разрезан между ними."""
    bal = 0
    for m in _JINJA_CTRL_RE.finditer(html or ""):
        tag = m.group(1).lower()
        if tag.startswith("end") or tag in ("else", "elif"):
            if tag.startswith("end"):
                bal -= 1
        elif tag not in ("set",):  # {% set x = 1 %} самодостаточен
            bal += 1
    return bal


def _html_chunks_need_join(blocks: list[dict]) -> bool:
    """True, если среди html-блоков есть незакрытый Jinja-цикл/условие —
    рендерить по одному нельзя (TemplateSyntaxError → пустая подборка)."""
    bal = 0
    for b in blocks or []:
        if (b or {}).get("type") != "html":
            continue
        bal += _jinja_ctrl_balance(b.get("html") or "")
        if bal != 0:
            return True
    return False


def _wrap_branded(body: str, lk: dict, unsub: str) -> str:
    return (
        f'<div style="font-family:sans-serif;max-width:640px;margin:0 auto;'
        f'border:1px solid #e6ebf3;border-radius:14px;overflow:hidden">'
        f'<div style="background:{lk["brand_color"]};color:#fff;padding:16px 24px;font-weight:700;font-size:18px">'
        f'{lk["header"]}</div>'
        f'<div style="padding:24px">{body}</div>'
        f'<div style="background:#f5f7fb;padding:16px 24px;font-size:12px;color:#888">'
        f'<a href="{unsub}" style="color:#888">{lk["footer"]}</a></div></div>'
    )


def _wrap_block_box(b: dict, html: str) -> str:
    if not html:
        return html
    box = []
    if _valid_color(b.get("bg")):
        box.append(f'background:{b["bg"]}')
    if _valid_color(b.get("border")):
        box.append(f'border:1px solid {b["border"]}')
    if box:
        box.append("padding:14px 18px;border-radius:10px")
    sp = _SPACE.get(b.get("space"))
    if sp:
        box.append(f"margin:{sp} 0")
    if box:
        return f'<div style="{";".join(box)}">{html}</div>'
    return html


def render_blocks(blocks: list[dict], products: list[dict], user_id: str,
                  campaign: str, look: dict | None = None) -> str:
    """Рендер письма из блоков конструктора. Шапка/футер берутся из look (единый бренд)."""
    lk = _look(look)
    unsub = f'{unsub_base()}?u={user_id}&c={campaign}'
    blocks = blocks or []
    # Импортированное письмо целиком (единственный блок) → отдаём документ, минуя брендовую обёртку.
    # MJML (type=mjml или содержимое с <mjml>) компилируем; сырой HTML отдаём как есть.
    if len(blocks) == 1:
        b0 = blocks[0] or {}
        raw = b0.get("mjml") or b0.get("html") or ""
        if b0.get("type") == "mjml" or "<mjml" in raw[:2000].lower():
            return render_mjml(raw, products, user_id, campaign, look)
        if b0.get("type") == "html":
            return render_html_template(raw, products, user_id, campaign, look)
    # Импорт, разбитый на секции: все блоки html → склеиваем как есть, без брендовой обёртки
    # (у письма своя шапка/футер). Так «разбито по блокам», а вид остаётся 1-в-1.
    # Склейку прогоняем через Jinja-слой целиком: цикл товаров может быть разрезан на секции,
    # и тогда {% for %} и {% endfor %} лежат в разных блоках — по отдельности не отрендерятся.
    if blocks and all((b or {}).get("type") == "html" for b in blocks):
        return render_html_template("".join((b.get("html") or "") for b in blocks),
                                    products, user_id, campaign, look)
    # Смешанный шаблон: цикл {% for %}…{% endfor %} разрезан по html-блокам.
    if _html_chunks_need_join(blocks):
        parts = []
        for b in blocks:
            b = b or {}
            if b.get("type") == "html":
                parts.append(b.get("html") or "")
            else:
                parts.append(_wrap_block_box(b, _render_block(b, products, campaign, lk, user_id)))
        body = render_html_template("".join(parts), products, user_id, campaign, look)
        # render_html_template уже мог обернуть/вставить товары; если это полный документ —
        # не двойная брендовая обёртка. Здесь склейка даёт фрагмент → оборачиваем.
        if body.lstrip().lower().startswith(("<!doctype", "<html", "<mjml")):
            return body
        return _wrap_branded(body.replace("{{unsubscribe_url}}", unsub), lk, unsub)
    parts = []
    for b in blocks:
        html = _wrap_block_box(b or {}, _render_block(b or {}, products, campaign, lk, user_id))
        parts.append(html)
    body = "".join(parts).replace("{{unsubscribe_url}}", unsub)
    out = _wrap_branded(body, lk, unsub)
    return _inject_products_if_missing(out, products, campaign, look)


# Стартовый шаблон сценария (если в БД ничего не сохранено) — повторяет текущие письма.
DEFAULT_BLOCKS: dict[str, list[dict]] = {
    "best_offer": [
        {"type": "heading", "text": "Подборка для вас"},
        {"type": "products"},
    ],
    "cart": [
        {"type": "heading", "text": "Вы забыли товары в корзине"},
        {"type": "text", "html": "Оформите заказ, пока товары в наличии:"},
        {"type": "products"},
        {"type": "button", "text": "Вернуться в корзину", "url": "https://groster.me/cart"},
    ],
    "postsale": [
        {"type": "heading", "text": "Спасибо за покупку!"},
        {"type": "text", "html": "Возможно, вам подойдёт:"},
        {"type": "products"},
    ],
}


def _demo() -> None:
    """Self-check рендера товаров/фото (без БД и почты)."""
    P = [{"product_id": str(i), "name": f'Товар & "{i}"', "price": 71.6 if i == 1 else i,
          "image_url": f"https://static.groster.me/{i}.png" if i < 5 else None,
          "product_url": "" if i == 2 else f"https://groster.me/p/{i}"} for i in range(1, 8)]

    # Цена: копейки не теряются (было int(71.6) == 71), формат как в админке, пробелы NBSP.
    assert _price(71.6) == "71,60\u00a0₽" and _price(12) == "12\u00a0₽"
    assert _price(1234.5) == "1\u00a0234,50\u00a0₽" and _price(None) == "0\u00a0₽"
    # Пустой product_url → "#", а не href="?utm_source=…".
    assert _utm("", "cart") == "#" and _utm("https://a/b", "cart").endswith("utm_campaign=cart")

    card = _card(P[0], "cart", False, LOOK_DEFAULTS)
    assert "71,60\u00a0₽" in card                              # цена с копейками
    assert '&amp; &quot;1&quot;' in card                        # имя экранировано (кавычки в alt)
    assert 'alt="Товар' in card                                 # битое фото → видно название
    # Относительный /img/… из БД → абсолютный src в письме (почтовик не знает хост сервиса).
    assert img_src("/img/x.png").endswith("/img/x.png") and img_src("/img/x.png").startswith("http")
    assert img_src("https://cdn.example/a.jpg") == "https://cdn.example/a.jpg"
    assert img_src(None) == "" and img_src("") == ""
    # 7 товаров → 3 ряда по 3 (добиваем пустыми ячейками), иначе вёрстка уезжает за 640px.
    grid = _cards_table(P, "cart", LOOK_DEFAULTS)
    assert grid.count('width="180"') == 9          # 7 карточек + 2 спейсера
    assert grid.count(LOOK_DEFAULTS["button"]) == 7
    # Длинное название обрезается в подписи, полный текст остаётся в alt.
    long_p = {**P[0], "name": "Пакет Майка ПНД 30*50 15мкм черный (100шт.) в пластах 1/10"}
    long_card = _card(long_p, "cart", False, LOOK_DEFAULTS)
    assert "…" in long_card and 'height="150"' in long_card and 'height="52"' in long_card

    # Импортированное письмо, разрезанное на секции: цикл товаров ЖИВОЙ (был текстом в письме).
    secs = [{"type": "html", "html": '<div>{% for item in get_cart_items() %}<img src="{{ item.picture }}">'},
            {"type": "html", "html": '<b>{{ item.name }}</b>{% endfor %}</div>'}]
    out = render_blocks(secs, P[:2], "u1", "cart")
    assert "{% for" not in out and "{{ item" not in out, out
    assert "static.groster.me/1.png" in out and "Товар &amp;" in out
    # Ссылка товара в импортированном шаблоне несёт UTM (как нативная карточка).
    linked = render_blocks(
        [{"type": "html", "html": '{% for item in get_recommendations() %}<a href="{{ item.url }}">x</a>{% endfor %}'}],
        P[:1], "u1", "best_offer")
    assert "utm_source=trigger" in linked and "utm_campaign=best_offer" in linked, linked
    assert "https://groster.me/p/1" in linked

    # html-блок рядом с нативными блоками — тоже через Jinja + подстановка отписки.
    mixed = render_blocks([{"type": "heading", "text": "Привет"},
                           {"type": "html", "html": '{% for i in get_x() %}<i>{{ i.name }}</i>{% endfor %}'
                                                    '<a href="{{unsubscribe_url}}">off</a>'}],
                          P[:1], "u9", "best_offer")
    assert "<i>Товар &amp;" in mixed and "unsubscribe?u=u9" in mixed and "{%" not in mixed

    # LeadHit без скобок: {% for item in get_recommendations %}
    bare = render_html_template(
        '<h2>Подборка товаров для вас</h2>{% for item in get_recommendations %}'
        '<div>{{ item.name }}</div>{% endfor %}', P[:2], "u1", "best_offer")
    assert "Товар &amp;" in bare and "{%" not in bare, bare

    # «Замороженный» HTML после конвертации MJML: заголовок есть, цикла нет → вставляем карточки.
    frozen = (
        '<div style="background:#f5f5f5"><div style="font-weight:bold">'
        'Подборка товаров для вас</div></div>'
        '<div>Почему выбирают интернет-магазин</div>'
    )
    filled = render_html_template(frozen, P[:2], "u1", "best_offer")
    assert "Товар &amp;" in filled and "71,60" in filled, filled
    assert filled.index("Подборка") < filled.index("Товар") < filled.index("Почему")

    # Цикл разрезан между html-блоками с heading — товары всё равно появляются.
    split_mixed = render_blocks([
        {"type": "heading", "text": "Подборка товаров для вас"},
        {"type": "html", "html": '{% for item in get_recommendations() %}<div class="p">{{ item.name }}</div>'},
        {"type": "html", "html": '{% endfor %}'},
    ], P[:2], "u1", "best_offer")
    assert "Товар &amp;" in split_mixed and "{% for" not in split_mixed, split_mixed

    print("templates._demo OK")


if __name__ == "__main__":
    _demo()
