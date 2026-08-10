"""Сервис 2 — Брошенная корзина (ТЗ раздел 3).

Событийный, near-real-time. session-ping (heartbeat «жив + корзина непуста», без истории
просмотров) → детект ухода с grace-period → gate-проверки → отправка.
Высший приоритет среди трёх сервисов; кап 1 письмо / 3 суток.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import app_settings, db, onec, svc_config
from app.config import settings
from app.mailer import get_mailer
from app.templates import DEFAULT_BLOCKS, render_blocks, render_email

router = APIRouter(tags=["cart"])

CANCELLED_STATUSES = {"cancelled", "canceled", "returned", "refunded"}

_STATIC = os.path.join(os.path.dirname(__file__), "static")
_TRIGGER_JS = os.path.join(_STATIC, "trigger.js")
_TRACK_JS = os.path.join(_STATIC, "track.js")
_DEMO_HTML = os.path.join(_STATIC, "demo.html")
_WHEEL_JS = os.path.join(_STATIC, "wheel.js")
_WHEEL_HTML = os.path.join(_STATIC, "wheel.html")

# Простая проверка email на границе доверия: не строгий RFC, а «есть чему слать письмо».
# Настоящая верификация — доставка письма; тут отсекаем явный мусор (без @, без домена).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(email: Optional[str]) -> bool:
    return bool(email) and _EMAIL_RE.match(email.strip()) is not None and len(email) <= 254


@router.get("/trigger.js")
async def trigger_js() -> FileResponse:
    """Триггер-сниппет для встраивания на groster.me (session-ping корзины)."""
    return FileResponse(
        _TRIGGER_JS,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/track.js")
async def track_js() -> FileResponse:
    """Универсальный загрузчик-трекер (аналог track.leadhit.io): один тег с clid грузит
    trigger.js/wheel.js и включает авто-захват email из форм. См. docs/site_integration.md."""
    return FileResponse(
        _TRACK_JS,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/demo")
async def demo_page() -> FileResponse:
    """Dev/reference: эталон разводки cart()/identify() (docs/site_integration.md)."""
    return FileResponse(_DEMO_HTML, headers={"Cache-Control": "no-cache"})


@router.get("/wheel.js")
async def wheel_js() -> FileResponse:
    """Виджет «Колесо фортуны» для встраивания на groster.me."""
    return FileResponse(
        _WHEEL_JS,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/wheel")
async def wheel_page() -> FileResponse:
    """Готовая страница-лендинг колеса (email-gate → скидка)."""
    return FileResponse(_WHEEL_HTML, headers={"Cache-Control": "no-cache"})


@router.get("/wheel-banner")
async def wheel_banner() -> FileResponse:
    """Готовый баннер-блок для встраивания на сайт (клик → попап колеса)."""
    return FileResponse(os.path.join(_STATIC, "wheel-banner.html"),
                        headers={"Cache-Control": "no-cache"})


@router.get("/wheel-config")
async def wheel_config() -> dict:
    """Публичный конфиг виджета (призы + тексты). Виджет тянет его при открытии,
    поэтому правки из админки видны без передеплоя фронта."""
    async with db.pool().acquire() as con:
        return await app_settings.wheel_config(con)


class WheelLead(BaseModel):
    email: str
    consent: bool = False          # галочка «согласен на обработку ПД / письма» (152-ФЗ)
    session_id: Optional[str] = None


@router.post("/wheel-lead")
async def wheel_lead(lead: WheelLead) -> dict:
    """Захват лида ДО выдачи скидки: валидный email + явное согласие → подписчик.

    Гейт «1 прокрут на пользователя»: помечаем wheel_spun_at атомарно. Повтор того же
    email → 409 (клиент блокирует колесо). Бонус-сектор «Ещё разок» повторный POST не шлёт,
    так что сервер честно считает одну попытку на email вне зависимости от бонус-прокрутов.
    """
    if not lead.consent:
        raise HTTPException(status_code=422, detail="consent_required")
    email = lead.email.strip()
    if not valid_email(email):
        raise HTTPException(status_code=422, detail="invalid_email")
    uid = anon_user_id(email)
    async with db.pool().acquire() as con:
        # Одна инструкция: ставит метку И определяет «уже крутил» (пустой RETURNING → 409).
        # WHERE ... IS NULL не даёт перезаписать метку у повторного захода — гонки нет.
        row = await con.fetchrow(
            """INSERT INTO subscribers(user_id, email, consent_at, wheel_spun_at)
               VALUES($1, $2, now(), now())
               ON CONFLICT (user_id) DO UPDATE SET
                 email = COALESCE(EXCLUDED.email, subscribers.email),
                 consent_at = COALESCE(subscribers.consent_at, EXCLUDED.consent_at),
                 wheel_spun_at = now()
               WHERE subscribers.wheel_spun_at IS NULL
               RETURNING wheel_spun_at""",
            uid, email,
        )
    if row is None:  # конфликт с уже крутившим email
        raise HTTPException(status_code=409, detail="already_spun")
    return {"ok": True}


class WheelPrize(BaseModel):
    email: str
    code: str
    label: Optional[str] = None


def _wheel_prize_html(code: str, label: Optional[str]) -> str:
    """Простое брендированное письмо с промокодом. Без каталога — только код и CTA."""
    site = settings.public_base_url.rstrip("/")
    title = label or "Ваш приз"
    return f"""\
<div style="font-family:Montserrat,Arial,sans-serif;max-width:520px;margin:0 auto;color:#3a1152">
  <div style="background:linear-gradient(135deg,#bc39e5,#6a12a0);border-radius:20px;padding:28px;text-align:center;color:#fff">
    <div style="font-size:22px;font-weight:800">🎉 {title}</div>
    <p style="margin:10px 0 18px;opacity:.92">Ваш персональный промокод на заказ в Гростер:</p>
    <div style="display:inline-block;background:#fff;color:#3a1152;font:800 22px/1 monospace;
                letter-spacing:2px;padding:14px 22px;border-radius:12px;border:2px dashed #fecc00">{code}</div>
    <p style="margin:18px 0 0"><a href="{site}" style="color:#fecc00;font-weight:700">Перейти в магазин →</a></p>
  </div>
  <p style="font-size:12px;color:#888;text-align:center;margin-top:14px">
    Письмо отправлено, потому что вы согласились на рассылку при участии в розыгрыше.</p>
</div>"""


@router.post("/wheel-prize")
async def wheel_prize(p: WheelPrize) -> dict:
    """Отправка выигранного промокода на email, указанный при прокруте.

    Код уже показан на экране (клиент выбирает сектор), так что письмо — копия для
    удобства, а не новая тайна. Шлём только тем, кто реально крутил (wheel_spun_at),
    и один раз (wheel_prize_code как идемпотентный «слот» — не спамим на ретраях/гонке).
    """
    email = p.email.strip()
    if not valid_email(email):
        raise HTTPException(status_code=422, detail="invalid_email")
    code = (p.code or "").strip()
    if not code:
        raise HTTPException(status_code=422, detail="no_code")
    uid = anon_user_id(email)
    async with db.pool().acquire() as con:
        sub = await con.fetchrow(
            "SELECT wheel_spun_at, wheel_prize_code FROM subscribers WHERE user_id = $1", uid)
        if sub is None or sub["wheel_spun_at"] is None:
            raise HTTPException(status_code=409, detail="not_spun")  # приз без прокрута не шлём
        if sub["wheel_prize_code"]:
            return {"ok": True, "already_sent": True}                # идемпотентно
        # Атомарно занимаем слот отправки: гонка/ретрай не дадут второго письма.
        claimed = await con.fetchval(
            """UPDATE subscribers SET wheel_prize_code = $2
               WHERE user_id = $1 AND wheel_prize_code IS NULL
               RETURNING TRUE""",
            uid, code)
        if not claimed:
            return {"ok": True, "already_sent": True}
    ok = await get_mailer().send(
        email, "Ваш промокод от Гростер 🎡", _wheel_prize_html(code, p.label),
        settings.mail_from, settings.mail_from_name, meta={"kind": "wheel_prize", "code": code})
    if not ok:
        # Письмо не ушло — освобождаем слот, чтобы клиент мог повторить.
        async with db.pool().acquire() as con:
            await con.execute("UPDATE subscribers SET wheel_prize_code = NULL WHERE user_id = $1", uid)
        raise HTTPException(status_code=502, detail="mail_failed")
    return {"ok": True}


class CartItem(BaseModel):
    """Позиция корзины из пинга. Обязателен только product_id — цену/фото/название письмо
    берёт из каталога (`products`), так что требовать их от витрины незачем.
    category_id/price принимаем «на вырост» (подбор по категории корзины), но не требуем."""
    product_id: str
    category_id: Optional[str] = None
    price: Optional[float] = None
    qty: int = 1


class Ping(BaseModel):
    session_id: str
    user_id: Optional[str] = None
    email: Optional[str] = None
    cart_items: list[CartItem]
    cart_hash: Optional[str] = None
    consent: Optional[bool] = None  # явное согласие с оформления (152-ФЗ) → завести подписчика


def anon_user_id(email: str) -> str:
    """Синтетический PK для анонима, согласившегося одним email (без user_id магазина).
    Детерминирован и регистронезависим → повторное согласие идемпотентно (ON CONFLICT)."""
    return "anon:" + email.strip().lower()


@router.post("/cart-ping")
async def cart_ping(ping: Ping) -> dict:
    """Heartbeat от фронтенда. Возврат в пределах grace → сессия снова active (ложный уход)."""
    items = json.dumps([i.model_dump() for i in ping.cart_items])
    async with db.pool().acquire() as con:
        await con.execute(
            """INSERT INTO cart_sessions(session_id, user_id, email, cart_items, cart_hash,
                                         state, last_ping_at)
               VALUES($1, $2, $3, $4::jsonb, $5, 'active', now())
               ON CONFLICT (session_id) DO UPDATE SET
                 user_id = COALESCE(EXCLUDED.user_id, cart_sessions.user_id),
                 email = COALESCE(EXCLUDED.email, cart_sessions.email),
                 cart_items = EXCLUDED.cart_items,
                 cart_hash = EXCLUDED.cart_hash,
                 last_ping_at = now(),
                 state = 'active',            -- вернулся → сброс возможного 'departed'
                 departed_at = NULL""",
            ping.session_id, ping.user_id, ping.email, items, ping.cart_hash,
        )
        # Явное согласие с оформления → заводим/обновляем подписчика (152-ФЗ).
        # Консервативно: первую дату согласия храним, is_unsubscribed НЕ трогаем
        # (иначе heartbeat «воскресил» бы отписавшегося).
        if ping.email and ping.consent:
            # ponytail: anon:email как PK — если тот же email позже придёт с реальным
            # user_id из фида, будет 2 строки (слияние личностей — отдельная задача).
            uid = ping.user_id or anon_user_id(ping.email)
            await con.execute(
                """INSERT INTO subscribers(user_id, email, consent_at)
                   VALUES($1, $2, now())
                   ON CONFLICT (user_id) DO UPDATE SET
                     email = COALESCE(EXCLUDED.email, subscribers.email),
                     consent_at = COALESCE(subscribers.consent_at, EXCLUDED.consent_at)""",
                uid, ping.email,
            )
        # Обратная связь витрине: какие product_id мы не нашли в каталоге. Такое письмо
        # уйдёт без товарного блока (см. gate unknown_products), поэтому лучше, чтобы
        # интегратор увидел опечатку сразу в devtools, а не через неделю по нулям в KPI.
        unknown: list[str] = []
        if ping.cart_items:
            ids = list({i.product_id for i in ping.cart_items})
            rows = await con.fetch(
                "SELECT product_id FROM products WHERE product_id = ANY($1::text[])", ids)
            known = {r["product_id"] for r in rows}
            unknown = [pid for pid in ids if pid not in known]
    return {"ok": True, "unknown": unknown}


def cart_gate(
    has_items: bool, has_email: bool, order_placed: bool,
    within_cooldown: bool, unsubscribed: bool, products_found: bool = True,
) -> Optional[str]:
    """Gate-проверки перед отправкой (ТЗ 3.3). None = можно слать, иначе причина skip."""
    if not has_items:
        return "empty_cart"
    if not products_found:
        # Ни один product_id из пинга не найден в каталоге → письмо «вы забыли товары»
        # ушло бы с пустым блоком. Это поломка интеграции/фида, а не повод спамить.
        return "unknown_products"
    if not has_email:
        return "no_email"
    if order_placed:
        return "order_placed"
    if unsubscribed:
        return "unsubscribed"
    if within_cooldown:
        return "cooldown"
    return None


async def run_due(con, mailer=None, force: bool = False) -> int:
    """Один тик near-real-time воркера. Возвращает число отправленных писем."""
    cfg = await svc_config.load(con, "cart")
    if not force and not cfg["enabled"]:
        return 0
    mailer = mailer or get_mailer()

    # Фаза 1: активные без heartbeat дольше таймаута и с непустой корзиной → departed.
    await con.execute(
        """UPDATE cart_sessions SET state = 'departed', departed_at = now()
           WHERE state = 'active'
             AND now() - last_ping_at > make_interval(secs => $1)
             AND jsonb_array_length(cart_items) > 0""",
        cfg["depart_timeout_sec"],
    )

    # Фаза 2: departed дольше grace и не вернулись → обработка.
    departed = await con.fetch(
        """SELECT session_id, user_id, email, cart_items, created_at
           FROM cart_sessions
           WHERE state = 'departed'
             AND now() - departed_at > make_interval(secs => $1)
           FOR UPDATE SKIP LOCKED""",
        cfg["grace_sec"],
    )
    look = await app_settings.template_look(con)
    tpl = await app_settings.active_template(con, "cart")
    blocks = tpl["blocks"] if tpl else DEFAULT_BLOCKS.get("cart")
    template_id = tpl["id"] if tpl else None
    sent = 0
    for s in departed:
        if await _process(con, s, mailer, cfg, look, blocks, template_id):
            sent += 1
    return sent


async def _order_placed(con, user_id, email, since) -> bool:
    """Оформлен ли заказ после старта сессии (в т.ч. «в 1 клик»). ТЗ 3.2.

    Если 1С настроена — спрашиваем её (источник истины по заказам); иначе локальная БД.
    """
    if onec.configured() and user_id:
        try:
            r = await onec.order_exists(user_id, since.isoformat())
            return bool(r.get("exists"))
        except Exception as e:  # noqa: BLE001 — при сбое 1С падаем на локальную проверку
            print(f"[onec] order_exists fail: {e}")
    return await con.fetchval(
        """SELECT EXISTS(
             SELECT 1 FROM orders
             WHERE order_date >= $3
               AND (user_id = $1 OR ($2::text IS NOT NULL AND email = $2))
               AND lower(status) <> ALL($4::text[]))""",
        user_id, email, since, list(CANCELLED_STATUSES),
    )


async def _process(con, s, mailer, cfg, look, blocks=None, template_id=None) -> bool:
    cooldown_hours = cfg["cooldown_hours"]
    # Резолвим подписчика по user_id или email (нужен для капа и email_log.user_id).
    sub = await con.fetchrow(
        """SELECT user_id, email, is_unsubscribed, last_sent_cart_at
           FROM subscribers
           WHERE user_id = $1 OR ($2::text IS NOT NULL AND email = $2)
           LIMIT 1""",
        s["user_id"], s["email"],
    )
    # Истина по составу корзины — последний ping сниппета (решение п.2): 1С данные
    # отдаёт файлом (каталог/топ-5/подписчики), живого запроса корзины по session_id нет.
    # Пустую/оформленную корзину закрывают gate has_items и проверка заказа ниже.
    items = json.loads(s["cart_items"])
    email = s["email"] or (sub["email"] if sub else None)

    within_cooldown = bool(
        sub and sub["last_sent_cart_at"] is not None and
        await con.fetchval(
            "SELECT (now() - $1) < make_interval(hours => $2)",
            sub["last_sent_cart_at"], cooldown_hours,
        )
    )
    order_placed = await _order_placed(con, s["user_id"], s["email"], s["created_at"])
    products = await _load_products(con, [i["product_id"] for i in items])

    reason = cart_gate(
        has_items=len(items) > 0,
        has_email=bool(email) and sub is not None,  # нужен subscriber для email_log
        order_placed=order_placed,
        within_cooldown=within_cooldown,
        unsubscribed=bool(sub and sub["is_unsubscribed"]),
        products_found=bool(products),
    )
    if reason is not None:
        await con.execute(
            "UPDATE cart_sessions SET state = 'converted' WHERE session_id = $1"
            if reason == "order_placed" else
            "UPDATE cart_sessions SET state = 'sent' WHERE session_id = $1",  # закрываем, не ретраим
            s["session_id"],
        )
        return False

    # Двойная проверка заказа непосредственно перед отправкой (ТЗ 3.6, гонка письмо/заказ).
    if await _order_placed(con, s["user_id"], s["email"], s["created_at"]):
        await con.execute(
            "UPDATE cart_sessions SET state = 'converted' WHERE session_id = $1", s["session_id"]
        )
        return False

    if blocks:
        html = render_blocks(blocks, products, sub["user_id"], "cart", look)
    else:
        intro = "<h2>Вы забыли товары в корзине</h2><p>Оформите заказ, пока товары в наличии:</p>"
        html = render_email(intro, products, sub["user_id"], "cart", cfg.get("template", "default"), look)
    log_id = await con.fetchval(
        """INSERT INTO email_log(user_id, service, product_ids, template_id, status)
           VALUES($1, 'cart', $2, $3, 'queued') RETURNING id""",
        sub["user_id"], [i["product_id"] for i in items], template_id,
    )
    ok = await mailer.send(email, cfg["subject"], html,
                           cfg["sender_email"], cfg["sender_name"], meta={"log_id": log_id})
    if not ok:
        await con.execute("UPDATE email_log SET status='failed' WHERE id=$1", log_id)
        return False

    async with con.transaction():
        await con.execute("UPDATE email_log SET status='sent', sent_at=now() WHERE id=$1", log_id)
        await con.execute(
            "UPDATE subscribers SET last_sent_cart_at = now(), last_any_trigger_at = now() WHERE user_id = $1",
            sub["user_id"],
        )
        await con.execute(
            "UPDATE cart_sessions SET state = 'sent' WHERE session_id = $1", s["session_id"]
        )
    return True


async def _load_products(con, product_ids: list[str]) -> list[dict]:
    rows = await con.fetch(
        """SELECT product_id, name, price, image_url, product_url FROM products
           WHERE product_id = ANY($1::text[])""",
        product_ids,
    )
    by_id = {r["product_id"]: dict(r) for r in rows}
    return [dict(by_id[pid], price=float(by_id[pid]["price"])) for pid in product_ids if pid in by_id]


def _demo() -> None:
    """Self-check gate-логики (ТЗ 3.3). Порядок: пусто→нет в каталоге→email→заказ→отписка→кап."""
    assert cart_gate(True, True, False, False, False) is None          # всё ок → слать
    assert cart_gate(False, True, False, False, False) == "empty_cart"
    # Товары есть, но ни одного нет в каталоге → письмо было бы пустым.
    assert cart_gate(True, True, False, False, False, products_found=False) == "unknown_products"
    assert cart_gate(False, True, False, False, False, products_found=False) == "empty_cart"
    assert cart_gate(True, False, False, False, False) == "no_email"
    assert cart_gate(True, True, True, False, False) == "order_placed"  # заказ перебивает
    assert cart_gate(True, True, False, False, True) == "unsubscribed"
    assert cart_gate(True, True, False, True, False) == "cooldown"
    # Заказ важнее капа и отписки (проверяем приоритет).
    assert cart_gate(True, True, True, True, True) == "order_placed"
    # Синтетический id анонима: регистронезависим и идемпотентен.
    assert anon_user_id("Foo@Bar.ru") == anon_user_id(" foo@bar.ru ") == "anon:foo@bar.ru"
    # valid_email: отсекаем мусор на границе доверия, пропускаем нормальные адреса.
    assert valid_email("a@b.ru") and valid_email(" user.name+x@mail.example.com ")
    assert not valid_email(None) and not valid_email("") and not valid_email("noatsign")
    assert not valid_email("a@b") and not valid_email("a b@c.ru") and not valid_email("a@@b.ru")
    print("cart._demo OK")


if __name__ == "__main__":
    _demo()
