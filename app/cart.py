"""Сервис 2 — Брошенная корзина (ТЗ раздел 3).

Событийный, near-real-time. session-ping (heartbeat «жив + корзина непуста», без истории
просмотров) → детект ухода с grace-period → gate-проверки → отправка.
Высший приоритет среди трёх сервисов; кап 1 письмо / 3 суток.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app import app_settings, db, onec, svc_config
from app.mailer import get_mailer
from app.templates import DEFAULT_BLOCKS, render_blocks, render_email

router = APIRouter(tags=["cart"])

CANCELLED_STATUSES = {"cancelled", "canceled", "returned", "refunded"}


class CartItem(BaseModel):
    product_id: str
    category_id: str
    price: float
    qty: int = 1


class Ping(BaseModel):
    session_id: str
    user_id: Optional[str] = None
    email: Optional[str] = None
    cart_items: list[CartItem]
    cart_hash: Optional[str] = None


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
    return {"ok": True}


def cart_gate(
    has_items: bool, has_email: bool, order_placed: bool,
    within_cooldown: bool, unsubscribed: bool,
) -> Optional[str]:
    """Gate-проверки перед отправкой (ТЗ 3.3). None = можно слать, иначе причина skip."""
    if not has_items:
        return "empty_cart"
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
    items = json.loads(s["cart_items"])
    email = s["email"] or (sub["email"] if sub else None)

    # 1С — источник истины по составу корзины: обновляем перед отправкой.
    if onec.configured():
        try:
            fresh = await onec.fetch_cart(s["session_id"])
            if fresh.get("is_empty"):
                items = []
            elif fresh.get("cart_items") is not None:
                items = fresh["cart_items"]
            email = fresh.get("email") or email
        except Exception as e:  # noqa: BLE001 — при сбое 1С используем состав из ping
            print(f"[onec] fetch_cart fail: {e}")

    within_cooldown = bool(
        sub and sub["last_sent_cart_at"] is not None and
        await con.fetchval(
            "SELECT (now() - $1) < make_interval(hours => $2)",
            sub["last_sent_cart_at"], cooldown_hours,
        )
    )
    order_placed = await _order_placed(con, s["user_id"], s["email"], s["created_at"])

    reason = cart_gate(
        has_items=len(items) > 0,
        has_email=bool(email) and sub is not None,  # нужен subscriber для email_log
        order_placed=order_placed,
        within_cooldown=within_cooldown,
        unsubscribed=bool(sub and sub["is_unsubscribed"]),
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

    products = await _load_products(con, [i["product_id"] for i in items])
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
    """Self-check gate-логики (ТЗ 3.3). Порядок причин: пусто→email→заказ→отписка→кап."""
    assert cart_gate(True, True, False, False, False) is None          # всё ок → слать
    assert cart_gate(False, True, False, False, False) == "empty_cart"
    assert cart_gate(True, False, False, False, False) == "no_email"
    assert cart_gate(True, True, True, False, False) == "order_placed"  # заказ перебивает
    assert cart_gate(True, True, False, False, True) == "unsubscribed"
    assert cart_gate(True, True, False, True, False) == "cooldown"
    # Заказ важнее капа и отписки (проверяем приоритет).
    assert cart_gate(True, True, True, True, True) == "order_placed"
    print("cart._demo OK")


if __name__ == "__main__":
    _demo()
