"""Рендер email-шаблонов. Два макета (default/minimal) + настраиваемое оформление (look).

look: {brand_color, header, button, footer} — редактируется в разделе «Шаблоны писем».
Выбор макета задаётся в настройках сценария (cfg['template']).
"""
from __future__ import annotations

import html as _htmllib
import re
from html.parser import HTMLParser

from app.config import settings

# Публичный адрес сервиса из конфига (не хардкод домена) — ссылка отписки должна вести
# туда, где реально отвечает /unsubscribe.
UNSUB_BASE = settings.public_base_url.rstrip("/") + "/unsubscribe"

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
    sep = "&" if "?" in (url or "") else "?"
    return f"{url}{sep}utm_source=trigger&utm_campaign={campaign}"


def _card(p: dict, campaign: str, minimal: bool, lk: dict) -> str:
    url = _utm(p["product_url"], campaign)
    img = (f'<img src="{p["image_url"]}" width="150" style="max-width:150px;border-radius:8px" alt="">'
           if p.get("image_url") else '<div style="height:150px;background:#eef2f8;border-radius:8px"></div>')
    if minimal:
        return f'<tr><td style="padding:6px 0"><a href="{url}">{p["name"]}</a> — {int(p["price"])} ₽</td></tr>'
    return (
        f'<td width="180" style="padding:10px;text-align:center;vertical-align:top">'
        f'{img}<div style="font-weight:600;margin:8px 0 4px">{p["name"]}</div>'
        f'<div style="color:#555;margin-bottom:8px">{int(p["price"])} ₽</div>'
        f'<a href="{url}" style="display:inline-block;background:{lk["brand_color"]};color:#fff;'
        f'text-decoration:none;padding:8px 16px;border-radius:8px;font-size:14px">{lk["button"]}</a></td>'
    )


def render_email(intro_html: str, products: list[dict], user_id: str,
                 campaign: str, template: str = "default", look: dict | None = None) -> str:
    lk = _look(look)
    unsub = f'{UNSUB_BASE}?u={user_id}&c={campaign}'
    minimal = template == "minimal"

    if minimal:
        rows = "".join(_card(p, campaign, True, lk) for p in products)
        return (
            f'<div style="font-family:sans-serif;max-width:600px;color:#222">'
            f'{intro_html}<table style="width:100%">{rows}</table>'
            f'<p style="font-size:12px;color:#888;margin-top:16px">'
            f'<a href="{unsub}">{lk["footer"]}</a></p></div>'
        )

    cards = "".join(_card(p, campaign, False, lk) for p in products)
    return (
        f'<div style="font-family:sans-serif;max-width:640px;margin:0 auto;'
        f'border:1px solid #e6ebf3;border-radius:14px;overflow:hidden">'
        f'<div style="background:{lk["brand_color"]};color:#fff;padding:16px 24px;font-weight:700;font-size:18px">'
        f'{lk["header"]}</div>'
        f'<div style="padding:24px">{intro_html}'
        f'<table><tr>{cards}</tr></table></div>'
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


def _render_block(b: dict, products: list[dict], campaign: str, lk: dict) -> str:
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
        cards = "".join(_card(p, campaign, False, lk) for p in products)
        return f'<table role="presentation" style="margin:8px 0"><tr>{cards}</tr></table>'
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
        # Сырой HTML от админа (доверенный источник) — отдаём как есть, без санитизации:
        # это единственный способ сохранить табличную вёрстку готового письма.
        return b.get("html") or ""
    if t == "columns":
        left = sanitize_html(b.get("left") or "")
        right = sanitize_html(b.get("right") or "")
        return (f'<table role="presentation" width="100%" style="margin:10px 0"><tr>'
                f'<td width="50%" valign="top" style="padding:0 10px 0 0;color:#333;font-size:14px;line-height:1.5">{left}</td>'
                f'<td width="50%" valign="top" style="padding:0 0 0 10px;color:#333;font-size:14px;line-height:1.5">{right}</td>'
                f'</tr></table>')
    return ""


def _mjml_items(products: list[dict] | None) -> list[dict]:
    """Адаптер: наши товары → объекты, которых ждёт MJML-шаблон LeadHit.
    Шаблон обращается к item.url/picture/name/price и делит цену на 100 (LeadHit хранил
    копейки) — поэтому цену в рублях домножаем обратно."""
    out = []
    for p in (products or []):
        out.append({
            "url": p.get("product_url") or "#",
            "picture": p.get("image_url") or "",
            "name": p.get("name") or "",
            "price": int(round(float(p.get("price") or 0) * 100)),
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


def render_mjml(source: str, products: list[dict], user_id: str, campaign: str) -> str:
    """Импортированный MJML-шаблон (Jinja + MJML): рендерим Jinja с товарами/отпиской,
    затем компилируем MJML → HTML. Ошибку показываем баннером (превью в админке видит проблему
    до активации; ponytail: без сложной обработки ошибок — админ проверяет письмо глазами)."""
    import jinja2
    import mrml
    unsub = f'{UNSUB_BASE}?u={user_id}&c={campaign}'
    items = _mjml_items(products)
    # Разные шаблоны LeadHit зовут разные функции данных: get_recommendations(),
    # get_cart_items(), get_order_items(), get_viewed_items() и т.п. — все означают
    # «дай товары сценария». Находим все вызовы get_*() в шаблоне и отдаём им items
    # (ни одна питон/jinja-функция не начинается с get_, поэтому пересечений нет).
    ctx = {"unsubscribe_url": unsub}
    for name in set(re.findall(r'(?<![\w.])(get_[A-Za-z0-9_]*)\s*\(', source)):
        ctx[name] = lambda *a, **k: items
    ctx.setdefault("get_recommendations", lambda *a, **k: items)
    ctx.setdefault("get_cart_items", lambda *a, **k: items)
    # Не-товарные хелперы LeadHit: get_utc_time() возвращает строку времени (шаблоны таймеров
    # делают .split('+')), а не список — поэтому переопределяем поверх сканера.
    import datetime
    ctx["get_utc_time"] = lambda *a, **k: datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        # ChainableUndefined: неизвестные переменные/атрибуты (lead.name, alert_name, …)
        # рендерятся пустыми, а не роняют шаблон. Функции данных (get_*) заданы явно выше.
        env = jinja2.Environment(autoescape=False, undefined=jinja2.ChainableUndefined)
        mjml_str = env.from_string(source).render(**ctx)
        try:
            res = mrml.to_html(mjml_str)
        except Exception:                       # битая вёрстка → чиним теги и пробуем ещё раз
            res = mrml.to_html(_balance_html(mjml_str))
        return getattr(res, "content", res)
    except Exception as e:  # noqa: BLE001 — показываем причину в превью, не роняем воркер
        return (f'<div style="font-family:sans-serif;padding:24px;color:#b00020;line-height:1.5">'
                f'<b>Не удалось собрать MJML-шаблон.</b><br>'
                f'Скорее всего, ошибка в вёрстке исходника (незакрытые или лишние теги, '
                f'неподдерживаемый MJML-элемент). Откройте шаблон в mjml.io, исправьте вёрстку '
                f'и загрузите заново.<br><br><span style="color:#888;font-size:12px">Детали: '
                f'{_esc(type(e).__name__)}: {_esc(e)}</span></div>')


def render_blocks(blocks: list[dict], products: list[dict], user_id: str,
                  campaign: str, look: dict | None = None) -> str:
    """Рендер письма из блоков конструктора. Шапка/футер берутся из look (единый бренд)."""
    lk = _look(look)
    unsub = f'{UNSUB_BASE}?u={user_id}&c={campaign}'
    blocks = blocks or []
    # Импортированное письмо целиком (единственный блок) → отдаём документ, минуя брендовую обёртку.
    # MJML (type=mjml или содержимое с <mjml>) компилируем; сырой HTML отдаём как есть.
    if len(blocks) == 1:
        b0 = blocks[0] or {}
        raw = b0.get("mjml") or b0.get("html") or ""
        if b0.get("type") == "mjml" or "<mjml" in raw[:2000].lower():
            return render_mjml(raw, products, user_id, campaign)
        if b0.get("type") == "html":
            return raw.replace("{{unsubscribe_url}}", unsub)
    # Импорт, разбитый на секции: все блоки html → склеиваем как есть, без брендовой обёртки
    # (у письма своя шапка/футер). Так «разбито по блокам», а вид остаётся 1-в-1.
    if blocks and all((b or {}).get("type") == "html" for b in blocks):
        return "".join((b.get("html") or "") for b in blocks).replace("{{unsubscribe_url}}", unsub)
    parts = []
    for b in blocks:
        html = _render_block(b, products, campaign, lk)
        if html:
            box = []
            if _valid_color(b.get("bg")):
                box.append(f'background:{b["bg"]}')
            if _valid_color(b.get("border")):
                box.append(f'border:1px solid {b["border"]}')
            if box:  # фон/рамка → добавляем внутренние отступы и скругление
                box.append("padding:14px 18px;border-radius:10px")
            sp = _SPACE.get(b.get("space"))
            if sp:
                box.append(f"margin:{sp} 0")
            if box:
                html = f'<div style="{";".join(box)}">{html}</div>'
        parts.append(html)
    body = "".join(parts).replace("{{unsubscribe_url}}", unsub)
    return (
        f'<div style="font-family:sans-serif;max-width:640px;margin:0 auto;'
        f'border:1px solid #e6ebf3;border-radius:14px;overflow:hidden">'
        f'<div style="background:{lk["brand_color"]};color:#fff;padding:16px 24px;font-weight:700;font-size:18px">'
        f'{lk["header"]}</div>'
        f'<div style="padding:24px">{body}</div>'
        f'<div style="background:#f5f7fb;padding:16px 24px;font-size:12px;color:#888">'
        f'<a href="{unsub}" style="color:#888">{lk["footer"]}</a></div></div>'
    )


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
