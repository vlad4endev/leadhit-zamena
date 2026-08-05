"""Админ-панель (ТЗ 8.1): настройки сервисов (вкл/выкл, интервалы, cooldown),
просмотр логов/статусов, мониторинг фидов, ручной запуск/остановка.
KPI-дашборд — существующий /kpi.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from typing import Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app import analytics, app_settings, best_offer, cart, db, onec, postsale, svc_config

router = APIRouter(prefix="/admin", tags=["admin"])

# Ручной запуск: сервис → воркер (force=True обходит вкл/выкл для теста/донастройки).
_RUNNERS = {
    "postsale": lambda con: postsale.run_due(con, force=True),
    "best_offer": lambda con: best_offer.run_batch(con, force=True),
    "cart": lambda con: cart.run_due(con, force=True),
    "attribution": lambda con: analytics.run_attribution(con),
}


class ConfigPatch(BaseModel):
    enabled: Optional[bool] = None
    params: Optional[dict] = None


@router.get("/config")
async def get_config() -> list[dict]:
    rows = await db.pool().fetch("SELECT service, enabled, params FROM service_config ORDER BY service")
    out = []
    for r in rows:
        raw = r["params"] if isinstance(r["params"], dict) else json.loads(r["params"])
        # Мердж дефолтов: редактор всегда видит полный набор полей (отправитель/тема/тайминг).
        params = {**svc_config.DEFAULTS.get(r["service"], {}), **raw}
        out.append({"service": r["service"], "enabled": r["enabled"], "params": params})
    return out


@router.get("/settings")
async def get_settings() -> dict:
    """Глобальные настройки: редактируемые значения + метаданные + read-only статус интеграций."""
    from app.config import settings as env
    async with db.pool().acquire() as con:
        values = await app_settings.get(con)
        await onec.load_overrides(con)
        onec_token_set = bool(
            await con.fetchval("SELECT value FROM app_config WHERE key = 'onec_token'")) \
            or bool(env.onec_token)
    meta = {k: {"type": m[0], "group": m[1], "label": m[2], "restart": m[3]}
            for k, m in app_settings.EDITABLE.items()}
    return {
        "values": values, "meta": meta,
        "readonly": {
            "smtp_host": env.smtp_host or None, "smtp_configured": bool(env.smtp_host),
            # base_url/token правятся в админке (app_config) поверх .env; токен наружу не отдаём.
            "onec_base_url": onec.base_url() or None, "onec_configured": onec.configured(),
            "onec_token_set": onec_token_set,
        },
    }


@router.put("/settings")
async def put_settings(patch: dict) -> dict:
    async with db.pool().acquire() as con:
        await app_settings.set_many(con, patch)
    return {"ok": True}


@router.post("/onec-config")
async def put_onec_config(body: dict) -> dict:
    """Подключение к 1С (base_url + token) в app_config поверх .env. Токен write-only.

    Пустой base_url → удаляем оверрайд (фолбэк на .env). Токен меняем только если передан.
    """
    base_url = (body.get("base_url") or "").strip()
    token = body.get("token") or ""
    async with db.pool().acquire() as con:
        if base_url:
            await con.execute(
                """INSERT INTO app_config(key, value) VALUES('onec_base_url', $1::jsonb)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                json.dumps(base_url))
        else:
            await con.execute("DELETE FROM app_config WHERE key = 'onec_base_url'")
        if token:
            await con.execute(
                """INSERT INTO app_config(key, value) VALUES('onec_token', $1::jsonb)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                json.dumps(token))
        await onec.load_overrides(con)
    return {"ok": True, "onec_configured": onec.configured()}


@router.post("/onec-test")
async def onec_test() -> dict:
    """Проверка связи с 1С: минимальный запрос каталога сохранённым base_url/token."""
    async with db.pool().acquire() as con:
        await onec.load_overrides(con)
    if not onec.configured():
        return {"ok": False, "reason": "Base URL не задан (ни в админке, ни в .env)"}
    try:
        data = await onec.fetch_catalog(page=1, page_size=1)
    except Exception as e:  # noqa: BLE001 — причину показываем в UI (401, таймаут, DNS…)
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    return {"ok": True, "base_url": onec.base_url(), "total": data.get("total")}


@router.get("/queue")
async def queue() -> dict:
    """Состояние очереди отправки (send_queue) по сервисам и статусам."""
    rows = await db.pool().fetch(
        "SELECT service, state, count(*) AS n FROM send_queue GROUP BY service, state")
    states = ["scheduled", "queued", "sent", "cancelled", "failed"]
    by = {}
    for r in rows:
        by.setdefault(r["service"], {s: 0 for s in states})[r["state"]] = r["n"]
    pending = sum(v.get("scheduled", 0) + v.get("queued", 0) for v in by.values())
    return {"by_service": [{"service": s, **cnt} for s, cnt in sorted(by.items())],
            "pending_total": pending, "states": states}


@router.get("/leads")
async def leads(q: Optional[str] = None, status: Optional[str] = None,
                limit: int = 50, offset: int = 0) -> list[dict]:
    """Список лидов с поиском (email/id) и фильтром статуса — как «Все лиды» в LeadHit."""
    rows = await db.pool().fetch(
        """SELECT s.user_id, s.email, s.is_unsubscribed, s.consent_at,
                  s.last_purchase_at, s.last_purchase_category_id,
                  s.wheel_spun_at, s.wheel_prize_code,
                  (SELECT count(*) FROM orders o WHERE o.user_id = s.user_id) AS orders_count,
                  (SELECT COALESCE(SUM(
                       (SELECT COALESCE(SUM((i->>'price')::numeric*(i->>'qty')::int),0)
                          FROM jsonb_array_elements(o.items) i)),0)
                     FROM orders o WHERE o.user_id = s.user_id) AS total_spent,
                  (SELECT count(*) FROM email_log e
                     WHERE e.user_id = s.user_id AND e.sent_at IS NOT NULL) AS emails_count
           FROM subscribers s
           WHERE ($1::text IS NULL OR s.email ILIKE '%'||$1||'%' OR s.user_id ILIKE '%'||$1||'%')
             AND ($2::text IS NULL
                  OR ($2 = 'subscribed'   AND NOT s.is_unsubscribed)
                  OR ($2 = 'unsubscribed' AND s.is_unsubscribed)
                  OR ($2 = 'wheel'        AND s.wheel_spun_at IS NOT NULL))
           ORDER BY (s.last_purchase_at IS NOT NULL) DESC, s.last_purchase_at DESC NULLS LAST,
                    s.wheel_spun_at DESC NULLS LAST, s.user_id
           LIMIT $3 OFFSET $4""",
        q or None, status or None, min(limit, 500), max(offset, 0),
    )
    return [{"user_id": r["user_id"], "email": r["email"],
             "is_unsubscribed": r["is_unsubscribed"],
             "consent_at": _iso(r["consent_at"]),
             "last_purchase_at": _iso(r["last_purchase_at"]),
             "category": r["last_purchase_category_id"],
             "source": "wheel" if r["wheel_spun_at"] else "",  # источник лида (пока знаем только колесо)
             "wheel_spun_at": _iso(r["wheel_spun_at"]),
             "wheel_prize": r["wheel_prize_code"],
             "orders_count": r["orders_count"],
             "total_spent": float(r["total_spent"] or 0),
             "emails_count": r["emails_count"]} for r in rows]


@router.get("/lead/{user_id}")
async def lead(user_id: str) -> dict:
    """Карточка лида: профиль, история писем, заказы."""
    async with db.pool().acquire() as con:
        s = await con.fetchrow(
            """SELECT user_id, email, is_unsubscribed, consent_at, last_purchase_at,
                      last_purchase_category_id, rotation_pointer_category_id,
                      last_sent_best_offer_at, last_sent_cart_at, last_sent_postsale_at,
                      wheel_spun_at, wheel_prize_code
               FROM subscribers WHERE user_id = $1""", user_id)
        if s is None:
            return {"found": False}
        emails = await con.fetch(
            """SELECT id, service, status, category_id, product_ids,
                      sent_at, opened_at, clicked_at, attributed_order_id, revenue
               FROM email_log WHERE user_id = $1 ORDER BY id DESC LIMIT 50""", user_id)
        orders = await con.fetch(
            """SELECT order_id, order_date, status,
                      (SELECT COALESCE(SUM((i->>'price')::numeric*(i->>'qty')::int),0)
                         FROM jsonb_array_elements(items) i) AS total
               FROM orders WHERE user_id = $1 ORDER BY order_date DESC LIMIT 50""", user_id)
    return {
        "found": True,
        "profile": {
            "user_id": s["user_id"], "email": s["email"], "is_unsubscribed": s["is_unsubscribed"],
            "consent_at": _iso(s["consent_at"]), "last_purchase_at": _iso(s["last_purchase_at"]),
            "category": s["last_purchase_category_id"], "rotation": s["rotation_pointer_category_id"],
            "last_sent_best_offer": _iso(s["last_sent_best_offer_at"]),
            "last_sent_cart": _iso(s["last_sent_cart_at"]),
            "last_sent_postsale": _iso(s["last_sent_postsale_at"]),
            "source": "wheel" if s["wheel_spun_at"] else "",
            "wheel_spun_at": _iso(s["wheel_spun_at"]),
            "wheel_prize": s["wheel_prize_code"],
        },
        "emails": [{"id": e["id"], "service": e["service"], "status": e["status"],
                    "product_ids": list(e["product_ids"] or []),
                    "sent_at": _iso(e["sent_at"]), "opened": e["opened_at"] is not None,
                    "clicked": e["clicked_at"] is not None,
                    "order_id": e["attributed_order_id"],
                    "revenue": float(e["revenue"]) if e["revenue"] is not None else None} for e in emails],
        "orders": [{"order_id": o["order_id"], "order_date": _iso(o["order_date"]),
                    "status": o["status"], "total": float(o["total"] or 0)} for o in orders],
    }


@router.get("/audience")
async def audience() -> dict:
    """Сводка по подписчикам."""
    r = await db.pool().fetchrow(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE email IS NOT NULL) AS with_email,
                  count(*) FILTER (WHERE is_unsubscribed) AS unsubscribed,
                  count(*) FILTER (WHERE consent_at IS NOT NULL) AS consented,
                  count(*) FILTER (WHERE last_purchase_at IS NOT NULL) AS with_purchase
           FROM subscribers""")
    return dict(r)


@router.get("/trends/weekday")
async def trends_weekday(service: Optional[str] = None) -> list[dict]:
    """Агрегат метрик по дню недели отправки (isodow: 1=Пн..7=Вс). Всегда 7 строк."""
    rows = await db.pool().fetch(
        """SELECT extract(isodow FROM sent_at)::int AS dow,
                  count(*) FILTER (WHERE sent_at IS NOT NULL)      AS sent,
                  count(*) FILTER (WHERE delivered_at IS NOT NULL) AS delivered,
                  count(*) FILTER (WHERE opened_at IS NOT NULL)    AS opened,
                  count(*) FILTER (WHERE clicked_at IS NOT NULL)   AS clicked,
                  count(attributed_order_id)                       AS orders,
                  COALESCE(SUM(revenue), 0)                        AS revenue
           FROM email_log
           WHERE sent_at IS NOT NULL AND ($1::text IS NULL OR service = $1::service_kind)
           GROUP BY dow""",
        service,
    )
    by = {r["dow"]: r for r in rows}
    out = []
    for d in range(1, 8):
        r = by.get(d)
        out.append({
            "dow": d,
            "sent": r["sent"] if r else 0, "delivered": r["delivered"] if r else 0,
            "opened": r["opened"] if r else 0, "clicked": r["clicked"] if r else 0,
            "orders": r["orders"] if r else 0,
            "revenue": float(r["revenue"]) if r else 0.0,
        })
    return out


@router.get("/trends/hour")
async def trends_hour(service: Optional[str] = None) -> list[dict]:
    """Агрегат метрик по часу отправки (0..23, локальная зона БД). Всегда 24 строки."""
    rows = await db.pool().fetch(
        """SELECT extract(hour FROM sent_at)::int AS hour,
                  count(*) FILTER (WHERE sent_at IS NOT NULL)      AS sent,
                  count(*) FILTER (WHERE delivered_at IS NOT NULL) AS delivered,
                  count(*) FILTER (WHERE opened_at IS NOT NULL)    AS opened,
                  count(*) FILTER (WHERE clicked_at IS NOT NULL)   AS clicked,
                  count(attributed_order_id)                       AS orders,
                  COALESCE(SUM(revenue), 0)                        AS revenue
           FROM email_log
           WHERE sent_at IS NOT NULL AND ($1::text IS NULL OR service = $1::service_kind)
           GROUP BY hour""",
        service,
    )
    by = {r["hour"]: r for r in rows}
    out = []
    for h in range(24):
        r = by.get(h)
        out.append({
            "hour": h,
            "sent": r["sent"] if r else 0, "delivered": r["delivered"] if r else 0,
            "opened": r["opened"] if r else 0, "clicked": r["clicked"] if r else 0,
            "orders": r["orders"] if r else 0,
            "revenue": float(r["revenue"]) if r else 0.0,
        })
    return out


@router.get("/services-summary")
async def services_summary() -> list[dict]:
    """Сводка сценариев для списка (как «Авторассылки»): активность, даты, отправлено."""
    rows = await db.pool().fetch(
        """SELECT c.service, c.enabled, c.created_at,
                  (SELECT max(sent_at) FROM email_log e
                     WHERE e.service = c.service AND e.sent_at IS NOT NULL) AS last_activity,
                  (SELECT count(*) FROM email_log e WHERE e.service = c.service) AS sent
           FROM service_config c ORDER BY c.service"""
    )
    return [{"service": r["service"], "enabled": r["enabled"],
             "launched": _iso(r["created_at"]), "last_activity": _iso(r["last_activity"]),
             "sent": r["sent"]} for r in rows]


@router.get("/recommendations")
async def recommendations() -> dict:
    """Топ-5 товаров по категориям с деталями — то, что реально идёт в письма (Best Offer, Постпродажа)."""
    rows = await db.pool().fetch(
        """SELECT t.category_id, c.name AS category_name, c.sort_order, t.position,
                  t.product_id, t.updated_at,
                  p.name AS product_name, p.price, p.image_url, p.product_url, p.in_stock
           FROM top5_by_category t
           JOIN categories c ON c.category_id = t.category_id
           LEFT JOIN products p ON p.product_id = t.product_id
           ORDER BY c.sort_order, t.position""")
    cats: dict = {}
    for r in rows:
        cat = cats.setdefault(r["category_id"], {
            "category_id": r["category_id"], "category_name": r["category_name"],
            "updated_at": _iso(r["updated_at"]), "products": []})
        exists = r["product_name"] is not None
        cat["products"].append({
            "position": r["position"], "product_id": r["product_id"],
            "name": r["product_name"], "price": float(r["price"]) if r["price"] is not None else None,
            "image_url": r["image_url"], "product_url": r["product_url"],
            "in_stock": bool(r["in_stock"]) if exists else False, "exists": exists,
            # Не попадёт в письмо, если товара нет в каталоге или он не в наличии.
            "usable": exists and bool(r["in_stock"]),
        })
    out = list(cats.values())
    total_usable = sum(1 for c in out for p in c["products"] if p["usable"])
    total = sum(len(c["products"]) for c in out)
    return {"categories": out, "category_count": len(out),
            "position_count": total, "usable_count": total_usable}


@router.get("/kpi")
async def kpi() -> dict:
    """Гейтованный алиас публичного /kpi: админка ходит сюда, чтобы за edge (whitelist
    /admin*) метрики оставались приватными — публичный /kpi наружу не отдаётся."""
    return await analytics.kpi()


@router.get("/scenario-meta")
async def scenario_meta() -> dict:
    """Метаданные полей редактора + правила аудитории (read-only) для UI."""
    return {"field_meta": svc_config.FIELD_META, "audience": svc_config.AUDIENCE}


@router.put("/config/{service}")
async def patch_config(service: str, patch: ConfigPatch) -> dict:
    """Вкл/выкл (остановка сценария) и правка бизнес-параметров. Действует со следующего тика."""
    async with db.pool().acquire() as con:
        if patch.enabled is not None:
            await con.execute(
                "UPDATE service_config SET enabled = $2 WHERE service = $1", service, patch.enabled
            )
        if patch.params is not None:
            await con.execute(
                "UPDATE service_config SET params = params || $2::jsonb WHERE service = $1",
                service, json.dumps(patch.params),
            )
    return {"ok": True}


@router.get("/logs")
async def logs(service: Optional[str] = None, status: Optional[str] = None,
               limit: int = 50, offset: int = 0) -> list[dict]:
    rows = await db.pool().fetch(
        """SELECT id, user_id, service, status, category_id, product_ids, order_id,
                  attributed_order_id, revenue, sent_at
           FROM email_log
           WHERE ($1::text IS NULL OR service = $1::service_kind)
             AND ($2::text IS NULL OR status = $2::email_status)
           ORDER BY id DESC LIMIT $3 OFFSET $4""",
        service, status, min(limit, 500), max(offset, 0),
    )
    return [dict(r, revenue=float(r["revenue"]) if r["revenue"] is not None else None,
                 sent_at=r["sent_at"].isoformat() if r["sent_at"] else None) for r in rows]


@router.get("/feeds-status")
async def feeds_status() -> dict:
    """Мониторинг актуальности фидов (ТЗ 8.1): свежесть каталога и топ-5, объём данных."""
    async with db.pool().acquire() as con:
        return {
            "products": {
                "count": await con.fetchval("SELECT count(*) FROM products"),
                "last_updated": _iso(await con.fetchval("SELECT max(updated_at) FROM products")),
            },
            "top5": {
                "categories": await con.fetchval("SELECT count(DISTINCT category_id) FROM top5_by_category"),
                "last_updated": _iso(await con.fetchval("SELECT max(updated_at) FROM top5_by_category")),
            },
            "orders": {
                "count": await con.fetchval("SELECT count(*) FROM orders"),
                "last_order": _iso(await con.fetchval("SELECT max(order_date) FROM orders")),
            },
            "subscribers": await con.fetchval("SELECT count(*) FROM subscribers"),
        }


@router.post("/run/{service}")
async def run_now(service: str) -> dict:
    """Ручной запуск воркера (ТЗ 8.1)."""
    runner = _RUNNERS.get(service)
    if runner is None:
        return {"ok": False, "reason": f"unknown service: {service}"}
    async with db.pool().acquire() as con:
        n = await runner(con)
    return {"ok": True, "processed": n}


def _iso(dt):
    return dt.isoformat() if dt else None


async def _efficiency_rows(from_: Optional[str], to: Optional[str], service: Optional[str]):
    """Атрибутированные заказы за период (отчёт «Эффективность», ТЗ 6)."""
    return await db.pool().fetch(
        """SELECT e.attributed_order_id AS order_id, o.email, o.order_date,
                  e.revenue, e.service
           FROM email_log e JOIN orders o ON o.order_id = e.attributed_order_id
           WHERE e.attributed_order_id IS NOT NULL
             AND ($1::text IS NULL OR o.order_date >= $1::text::timestamptz)
             AND ($2::text IS NULL OR o.order_date < ($2::text::timestamptz + interval '1 day'))
             AND ($3::text IS NULL OR e.service = $3::service_kind)
           ORDER BY o.order_date DESC""",
        from_, to, service,
    )


@router.get("/report/efficiency")
async def efficiency(from_: Optional[str] = None, to: Optional[str] = None,
                     service: Optional[str] = None) -> dict:
    rows = await _efficiency_rows(from_, to, service)
    total = sum(float(r["revenue"] or 0) for r in rows)
    return {
        "count": len(rows),
        "total_revenue": total,
        "orders": [{"order_id": r["order_id"], "email": r["email"],
                    "order_date": r["order_date"].isoformat(),
                    "revenue": float(r["revenue"] or 0), "service": r["service"]} for r in rows],
    }


@router.get("/report/efficiency.csv")
async def efficiency_csv(from_: Optional[str] = None, to: Optional[str] = None,
                         service: Optional[str] = None) -> Response:
    import csv
    import io
    rows = await _efficiency_rows(from_, to, service)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Заказ", "Email", "Дата", "Сумма", "Сценарий"])
    for r in rows:
        w.writerow([r["order_id"], r["email"] or "", r["order_date"].strftime("%Y-%m-%d %H:%M"),
                    f'{float(r["revenue"] or 0):.2f}', r["service"]])
    data = "﻿" + buf.getvalue()  # BOM для корректной кириллицы в Excel
    return Response(content=data, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="efficiency.csv"'})


_INTROS = {
    "best_offer": "<h2>Подборка для вас</h2><p>Товары, которые могут вам понравиться:</p>",
    "cart": "<h2>Вы забыли товары в корзине</h2><p>Оформите заказ, пока товары в наличии:</p>",
    "postsale": "<h2>Спасибо за покупку!</h2><p>Возможно, вам подойдёт:</p>",
}


class TestEmail(BaseModel):
    email: str


@router.post("/scenario/{service}/test")
async def scenario_test(service: str, body: TestEmail) -> dict:
    """Тест-письмо сценария на указанный адрес (реальный рендер активного/стандартного шаблона)."""
    from app.mailer import get_mailer
    from app.templates import DEFAULT_BLOCKS, render_blocks
    if service not in ("best_offer", "cart", "postsale"):
        return {"ok": False, "reason": "unknown service"}
    async with db.pool().acquire() as con:
        cfg = await svc_config.load(con, service)
        look = await app_settings.template_look(con)
        tpl = await app_settings.active_template(con, service)
        rows = await con.fetch(
            "SELECT product_id, name, price, image_url, product_url FROM products WHERE in_stock LIMIT 4")
    products = [dict(r, price=float(r["price"])) for r in rows]
    blocks = tpl["blocks"] if tpl else DEFAULT_BLOCKS.get(service, [])
    html = render_blocks(blocks, products, "test", service, look)
    mailer = get_mailer()
    subject = "[ТЕСТ] " + cfg["subject"]
    if type(mailer).__name__ == "HttpMailer":
        # Через mailer-service шлём СИНХРОННО (/v1/send/sync): тест-кнопка должна показывать
        # реальный результат ESP, а не «queued» (иначе UI пишет «отправлен», а письмо падает в воркере).
        try:
            res = await asyncio.to_thread(_mailer_svc, "POST", "/v1/send/sync", {
                "to": body.email, "subject": subject, "html": html,
                "from_email": cfg["sender_email"], "from_name": cfg["sender_name"]})
        except Exception as e:  # noqa: BLE001 — сервис недоступен
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "live": True}
        return {"ok": bool(res.get("ok")), "error": res.get("error"),
                "live": await _mailer_is_live(mailer)}
    ok = await mailer.send(body.email, subject, html, cfg["sender_email"], cfg["sender_name"])
    return {"ok": ok, "live": await _mailer_is_live(mailer)}


@router.get("/scenario/{service}/reach")
async def scenario_reach(service: str) -> dict:
    """Охват «сейчас»: сколько получателей, если запустить сценарий в данный момент (без отправки)."""
    async with db.pool().acquire() as con:
        if service == "best_offer":
            cfg = await svc_config.load(con, "best_offer")
            rows = await best_offer._candidates(con, cfg["interval_days"], cfg["after_purchase_days"])
            n = len(rows)
        elif service == "postsale":
            n = await con.fetchval(
                """SELECT count(*) FROM send_queue
                   WHERE service = 'postsale' AND state = 'scheduled' AND run_after <= now()""")
        elif service == "cart":
            cfg = await svc_config.load(con, "cart")
            n = await con.fetchval(
                """SELECT count(*) FROM cart_sessions
                   WHERE state = 'departed' AND now() - departed_at > make_interval(secs => $1)""",
                cfg["grace_sec"])
        else:
            return {"reach": None, "reason": "unknown service"}
    return {"reach": n}


@router.get("/template-settings")
async def get_template_settings() -> dict:
    async with db.pool().acquire() as con:
        return await app_settings.template_look(con)


@router.put("/template-settings")
async def put_template_settings(patch: dict) -> dict:
    async with db.pool().acquire() as con:
        await app_settings.set_template_look(con, patch)
    return {"ok": True}


@router.get("/template/preview", response_class=HTMLResponse)
async def template_preview(id: Optional[int] = None, service: str = "best_offer",
                           brand_color: Optional[str] = None, header: Optional[str] = None,
                           button: Optional[str] = None, footer: Optional[str] = None) -> str:
    """Предпросмотр письма на реальных товарах. id → конкретный шаблон; иначе активный/дефолт сценария."""
    from app.templates import DEFAULT_BLOCKS, render_blocks
    async with db.pool().acquire() as con:
        look = await app_settings.template_look(con)
        if id is not None:
            row = await con.fetchrow("SELECT service, blocks FROM email_templates WHERE id = $1", id)
            if row:
                service = row["service"] or "best_offer"   # черновик без типа — рендерим в общем контексте
                blocks = json.loads(row["blocks"]) if isinstance(row["blocks"], str) else row["blocks"]
            else:
                blocks = None
        else:
            tpl = await app_settings.active_template(con, service)
            blocks = tpl["blocks"] if tpl else None
        rows = await con.fetch(
            "SELECT product_id, name, price, image_url, product_url FROM products WHERE in_stock LIMIT 4")
    override = {"brand_color": brand_color, "header": header, "button": button, "footer": footer}
    look = {**look, **{k: v for k, v in override.items() if v}}
    products = [dict(r, price=float(r["price"])) for r in rows]
    blocks = blocks if blocks else DEFAULT_BLOCKS.get(service, [])
    return render_blocks(blocks, products, "preview", service, look)


@router.post("/template/render", response_class=HTMLResponse)
async def template_render(body: dict) -> str:
    """Живой рендер письма из блоков конструктора (несохранённых) — для превью в редакторе."""
    from app.templates import render_blocks
    service = body.get("service", "best_offer")
    blocks = body.get("blocks") or []
    look = body.get("look")
    async with db.pool().acquire() as con:
        if look is None:
            look = await app_settings.template_look(con)
        rows = await con.fetch(
            "SELECT product_id, name, price, image_url, product_url FROM products WHERE in_stock LIMIT 4")
    products = [dict(r, price=float(r["price"])) for r in rows]
    return render_blocks(blocks, products, "preview", service, look)


_TPL_NAMES = {"best_offer": "Best Offer", "cart": "Брошенная корзина", "postsale": "Постпродажа"}
_SERVICES = ("best_offer", "cart", "postsale")


_DRAFT_BLOCKS = [{"type": "heading", "text": "Заголовок письма"}, {"type": "products"}]


def _tpl_row(r) -> dict:
    return {
        "id": r["id"], "service": r["service"],
        "name": r["name"] or (_TPL_NAMES.get(r["service"]) if r["service"] else "Без названия"),
        "is_active": r["is_active"], "used": r["used"],
        "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat(),
    }


@router.get("/templates")
async def list_templates() -> dict:
    """Библиотека: шаблоны по сценариям + черновики без типа. У каждого — активность и счётчик отправок."""
    async with db.pool().acquire() as con:
        rows = await con.fetch(
            """SELECT t.id, t.service, t.name, t.is_active, t.created_at, t.updated_at,
                      (SELECT count(*) FROM email_log e
                       WHERE e.template_id = t.id AND e.sent_at IS NOT NULL) AS used
               FROM email_templates t ORDER BY t.created_at""")
    by_svc = {s: [] for s in _SERVICES}
    drafts = []
    for r in rows:
        (by_svc[r["service"]] if r["service"] in by_svc else drafts).append(_tpl_row(r))
    services = [{
        "service": s, "ru": _TPL_NAMES[s],
        "using_default": not any(t["is_active"] for t in by_svc[s]),
        "templates": by_svc[s],
    } for s in _SERVICES]
    return {"services": services, "drafts": drafts}


@router.post("/templates")
async def create_template(body: dict) -> dict:
    """Создать шаблон. service задан → прикреплён к сценарию (первый становится активным);
    service пуст → черновик. copy_from копирует блоки существующего шаблона."""
    from app.templates import DEFAULT_BLOCKS
    service = body.get("service") or None
    if service is not None and service not in DEFAULT_BLOCKS:
        return {"ok": False, "reason": "unknown service"}
    name = (body.get("name") or "").strip() or "Новый шаблон"
    async with db.pool().acquire() as con:
        blocks = None
        if body.get("copy_from"):
            blocks = await con.fetchval("SELECT blocks FROM email_templates WHERE id = $1", int(body["copy_from"]))
            if isinstance(blocks, str):
                blocks = json.loads(blocks)
        if blocks is None:
            blocks = DEFAULT_BLOCKS[service] if service else _DRAFT_BLOCKS
        active = False
        if service is not None:
            has_active = await con.fetchval(
                "SELECT EXISTS(SELECT 1 FROM email_templates WHERE service = $1 AND is_active)", service)
            active = not has_active
        new_id = await con.fetchval(
            """INSERT INTO email_templates(service, name, blocks, is_active)
               VALUES($1, $2, $3::jsonb, $4) RETURNING id""",
            service, name, json.dumps(blocks), active)
    return {"ok": True, "id": new_id}


@router.post("/template/{tid}/attach")
async def attach_template(tid: int, body: dict) -> dict:
    """Прикрепить шаблон к сценарию (тип) или открепить (service пуст → черновик).
    Если у сценария ещё нет активного — прикреплённый становится активным."""
    from app.templates import DEFAULT_BLOCKS
    service = body.get("service") or None
    if service is not None and service not in DEFAULT_BLOCKS:
        return {"ok": False, "reason": "unknown service"}
    async with db.pool().acquire() as con:
        exists = await con.fetchval("SELECT EXISTS(SELECT 1 FROM email_templates WHERE id = $1)", tid)
        if not exists:
            return {"ok": False, "reason": "not found"}
        async with con.transaction():
            if service is None:
                await con.execute(
                    "UPDATE email_templates SET service = NULL, is_active = false, updated_at = now() WHERE id = $1", tid)
            else:
                has_active = await con.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM email_templates WHERE service = $1 AND is_active AND id <> $2)",
                    service, tid)
                await con.execute(
                    "UPDATE email_templates SET service = $2, is_active = $3, updated_at = now() WHERE id = $1",
                    tid, service, not has_active)
    return {"ok": True}


@router.get("/template/{tid}")
async def get_template(tid: int) -> dict:
    async with db.pool().acquire() as con:
        r = await con.fetchrow(
            "SELECT id, service, name, blocks, is_active, created_at FROM email_templates WHERE id = $1", tid)
    if r is None:
        return {"ok": False, "reason": "not found"}
    blocks = json.loads(r["blocks"]) if isinstance(r["blocks"], str) else r["blocks"]
    return {"ok": True, "id": r["id"], "service": r["service"], "name": r["name"] or "",
            "blocks": blocks, "is_active": r["is_active"], "created_at": r["created_at"].isoformat()}


@router.put("/template/{tid}")
async def put_template(tid: int, body: dict) -> dict:
    blocks = body.get("blocks")
    if not isinstance(blocks, list):
        return {"ok": False, "reason": "bad request"}
    name = (body.get("name") or "").strip()
    async with db.pool().acquire() as con:
        res = await con.execute(
            """UPDATE email_templates
               SET blocks = $2::jsonb, name = COALESCE(NULLIF($3, ''), name), updated_at = now()
               WHERE id = $1""",
            tid, json.dumps(blocks), name)
    return {"ok": res.endswith("1")}


@router.post("/template/{tid}/activate")
async def activate_template(tid: int) -> dict:
    """Сделать шаблон активным для его сценария (снимает активность с остальных)."""
    async with db.pool().acquire() as con:
        service = await con.fetchval("SELECT service FROM email_templates WHERE id = $1", tid)
        if service is None:
            return {"ok": False, "reason": "не прикреплён к сценарию"}
        async with con.transaction():
            await con.execute("UPDATE email_templates SET is_active = false WHERE service = $1", service)
            await con.execute("UPDATE email_templates SET is_active = true WHERE id = $1", tid)
    return {"ok": True}


@router.post("/template/{tid}/delete")
async def delete_template(tid: int) -> dict:
    """Удалить шаблон. Если был активным — сценарий вернётся к стандартному шаблону."""
    async with db.pool().acquire() as con:
        await con.execute("DELETE FROM email_templates WHERE id = $1", tid)
    return {"ok": True}


# ── Загрузка изображений для конструктора (base64 JSON, без python-multipart) ──
_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads")
_IMG_SIG = {  # магические байты → допустимые расширения (лёгкая проверка, что это картинка)
    b"\x89PNG\r\n\x1a\n": {"png"},
    b"\xff\xd8\xff": {"jpg", "jpeg"},
    b"GIF87a": {"gif"}, b"GIF89a": {"gif"},
}


def _sniff_image(blob: bytes, ext: str) -> bool:
    if ext == "webp":
        return blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"
    return any(blob.startswith(sig) and ext in exts for sig, exts in _IMG_SIG.items())


@router.post("/upload")
async def upload_image(body: dict) -> dict:
    """Приём картинки как data-URI/base64 → файл в static/uploads → URL для блока. Лимит 3 МБ."""
    data = body.get("data") or ""
    m = re.match(r"data:image/(?P<ext>[\w.+-]+);base64,(?P<b64>.*)", data, re.S)
    if m:
        ext, raw = m.group("ext").lower(), m.group("b64")
    else:
        name = body.get("filename") or ""
        ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
        raw = data
    ext = "jpg" if ext == "jpeg" else ext
    if ext not in {"png", "jpg", "gif", "webp"}:
        return {"ok": False, "reason": "unsupported type"}
    try:
        blob = base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "bad data"}
    if not blob or len(blob) > 3_000_000:
        return {"ok": False, "reason": "too large"}
    if not _sniff_image(blob, ext):
        return {"ok": False, "reason": "not an image"}
    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    fn = hashlib.sha1(blob).hexdigest()[:16] + "." + ext
    with open(os.path.join(_UPLOAD_DIR, fn), "wb") as fh:
        fh.write(blob)
    # ponytail: путь относительный — в реальной рассылке письма нужен абсолютный URL публичного хоста
    return {"ok": True, "url": "/admin/uploads/" + fn}


@router.get("/uploads/{name}")
async def get_upload(name: str):
    """Отдаёт загруженную картинку. basename защищает от обхода каталога."""
    path = os.path.join(_UPLOAD_DIR, os.path.basename(name))
    if not os.path.isfile(path):
        return Response(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


def _mailer_svc(method: str, path: str, body: Optional[dict] = None) -> dict:
    """Синхронный вызов mailer-service (в потоке)."""
    import urllib.request
    from app.config import settings
    url = settings.mailer_service_url.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if settings.mailer_service_token:
        headers["Authorization"] = f"Bearer {settings.mailer_service_token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


async def _mailer_is_live(mailer) -> bool:
    """live = письмо реально уйдёт в ESP, а не осядет в dev-логе.

    Учитывает dev-режим самого mailer-service: HttpMailer != LogMailer ещё не значит,
    что сервис отправит — у него может быть пустой smtp_host (provider='dev').
    """
    name = type(mailer).__name__
    if name == "LogMailer":
        return False
    if name == "HttpMailer":
        try:
            cfg = await asyncio.to_thread(_mailer_svc, "GET", "/v1/config")
            return cfg.get("provider") == "smtp"
        except Exception:  # noqa: BLE001 — сервис недоступен → не рисуем ложный «отправлено»
            return False
    return True   # SmtpMailer — шлёт напрямую в SMTP-relay


@router.get("/mail/config")
async def mail_config() -> dict:
    """Настройки почтового провайдера (из mailer-service). Пароль не отдаётся."""
    from app.config import settings
    if not settings.mailer_service_url:
        return {"configured": False}
    try:
        cfg = await asyncio.to_thread(_mailer_svc, "GET", "/v1/config")
        return {"configured": True, "service_url": settings.mailer_service_url, **cfg}
    except Exception as e:  # noqa: BLE001
        return {"configured": True, "service_url": settings.mailer_service_url,
                "error": f"{type(e).__name__}: {e}"}


@router.put("/mail/config")
async def mail_config_save(patch: dict) -> dict:
    from app.config import settings
    if not settings.mailer_service_url:
        return {"ok": False, "reason": "mailer-service не подключён (MAILER_SERVICE_URL пуст)"}
    try:
        await asyncio.to_thread(_mailer_svc, "PUT", "/v1/config", patch)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


@router.post("/mail/test")
async def mail_test(body: dict) -> dict:
    from app.config import settings
    if not settings.mailer_service_url:
        return {"ok": False, "error": "mailer-service не подключён"}
    try:
        return await asyncio.to_thread(_mailer_svc, "POST", "/v1/test", {"to": body.get("to", "")})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── Колесо фортуны ──
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def _normalize_wheel(body: dict) -> dict:
    """Приводим присланный конфиг к безопасному виду перед записью.
    Правило секторов: пустой промокод → сектор «крутить ещё» (respin), а не битый приз."""
    from app.app_settings import WHEEL_DEFAULTS
    prizes = []
    for p in (body.get("prizes") or [])[:12]:      # кап 12 секторов — больше не читается на колесе
        label = str(p.get("label") or "").strip()[:40]
        if not label:
            continue                                # сектор без подписи пропускаем
        code = str(p.get("code") or "").strip()[:32]
        try:
            weight = max(0, int(p.get("weight") or 0))
        except (TypeError, ValueError):
            weight = 0
        color = p.get("color") if _HEX_RE.match(str(p.get("color") or "")) else "#bc39e5"
        prize = {"label": label, "code": code, "weight": weight, "color": color}
        if not code:
            prize["respin"] = True                  # без кода нечего выдавать → «Ещё разок»
        prizes.append(prize)

    def _int(key: str) -> int:
        try:
            return max(0, int(body.get(key, WHEEL_DEFAULTS[key])))
        except (TypeError, ValueError):
            return WHEEL_DEFAULTS[key]

    return {
        "enabled": bool(body.get("enabled", True)),
        "title": str(body.get("title") or "").strip()[:200] or WHEEL_DEFAULTS["title"],
        "subtitle": str(body.get("subtitle") or "").strip()[:300] or WHEEL_DEFAULTS["subtitle"],
        "once_days": _int("once_days"),
        "auto_delay_sec": _int("auto_delay_sec"),
        "prizes": prizes,
    }


@router.get("/wheel")
async def get_wheel() -> dict:
    async with db.pool().acquire() as con:
        return await app_settings.wheel_config(con)


@router.put("/wheel")
async def put_wheel(body: dict) -> dict:
    cfg = _normalize_wheel(body)
    if not cfg["prizes"]:
        return {"ok": False, "reason": "нужен хотя бы один сектор с подписью"}
    if sum(p["weight"] for p in cfg["prizes"]) == 0:
        return {"ok": False, "reason": "у секторов нулевые веса — колесо не сможет выбрать приз"}
    async with db.pool().acquire() as con:
        await app_settings.set_wheel_config(con, cfg)
    return {"ok": True}


@router.post("/sync-catalog")
async def sync_catalog_now() -> dict:
    """Ручная синхронизация каталога из 1С (pull)."""
    if not onec.configured():
        return {"ok": False, "reason": "1С не настроена (ONEC_BASE_URL пуст)"}
    async with db.pool().acquire() as con:
        n = await onec.sync_catalog(con)
    return {"ok": True, "synced": n}


@router.post("/import-xml")
async def import_xml_upload(request: Request) -> dict:
    """Импорт каталога/топ-5/подписчиков из XML (сырое тело файла). Атомарно."""
    from app import import_xml
    data = await request.body()
    try:
        parsed = import_xml.parse(data)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}
    async with db.pool().acquire() as con:
        counts = await import_xml.import_all(con, parsed)
    return {"ok": True, "imported": counts}


@router.get("/integration")
async def integration() -> dict:
    """Статус интеграции: режим отправки, домен, 1С. Секреты НЕ отдаём."""
    from app.config import settings
    from app.mailer import get_mailer
    mailer = type(get_mailer()).__name__
    async with db.pool().acquire() as con:
        await onec.load_overrides(con)
        last_ping = await con.fetchval("SELECT max(last_ping_at) FROM cart_sessions")
        sessions_today = await con.fetchval(
            "SELECT count(*) FROM cart_sessions WHERE last_ping_at >= date_trunc('day', now())")
    return {
        "mailer": mailer,
        "live": mailer != "LogMailer",
        "mail_from": settings.mail_from,
        "mail_from_name": settings.mail_from_name,
        "smtp_host": settings.smtp_host or None,
        "attribution_window_hours": settings.attribution_window_hours,
        "onec_configured": onec.configured(),
        "onec_base_url": onec.base_url() or None,
        # Сигнал «сайт на связи»: свежий пинг = track.js реально шлёт события.
        "last_ping_at": _iso(last_ping),
        "sessions_today": int(sessions_today or 0),
    }


_PAGE_PATH = os.path.join(os.path.dirname(__file__), "static", "admin.html")


@router.get("")
async def dashboard() -> FileResponse:
    return FileResponse(_PAGE_PATH, headers={"Cache-Control": "no-cache"})


def _demo() -> None:
    # Пустой код → respin; вес и цвет приводятся; сектор без подписи выпадает.
    out = _normalize_wheel({"prizes": [
        {"label": "Скидка 5%", "code": " GROSTER5 ", "weight": "10", "color": "#bc39e5"},
        {"label": "Ещё разок", "code": "", "weight": 2, "color": "oops"},
        {"label": "", "code": "X", "weight": 5},
    ]})
    assert [p["code"] for p in out["prizes"]] == ["GROSTER5", ""]
    assert out["prizes"][0]["weight"] == 10 and "respin" not in out["prizes"][0]
    assert out["prizes"][1].get("respin") is True and out["prizes"][1]["color"] == "#bc39e5"
    assert out["once_days"] == 7 and out["enabled"] is True
    print("admin._normalize_wheel OK")


if __name__ == "__main__":
    _demo()

