"""Этап 4 — аналитика (ТЗ раздел 6).

Приём вебхуков ESP (статусы письма), атрибуция дохода (клик↔заказ в окне),
6 метрик по каждому сервису рядом с бенчмарком LeadHit.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app import db
from app.config import settings

router = APIRouter(tags=["analytics"])

# Бенчмарк LeadHit (ТЗ 2.7 / 3.6 / 4.7) — цель сравнения на пилоте.
BENCHMARK = {
    "best_offer": {"deliverability": 0.971, "open": 0.087, "ctr": 0.0056, "conversion": 0.0171, "unsub": 0.0007, "avg_check": 5667},
    "cart":       {"deliverability": 0.907, "open": 0.135, "ctr": 0.0286, "conversion": 0.1983, "unsub": 0.0031, "avg_check": 5656},
    "postsale":   {"deliverability": 0.931, "open": 0.096, "ctr": 0.0060, "conversion": 0.1705, "unsub": 0.0011, "avg_check": 5228},
}

# Событие ESP → колонка времени. status держит последнее состояние.
_EVENT_COL = {
    "sent": "sent_at", "delivered": "delivered_at", "opened": "opened_at", "clicked": "clicked_at",
}


class Webhook(BaseModel):
    log_id: int
    event: str  # delivered|opened|clicked|bounced|unsubscribed


@router.post("/esp/webhook")
async def esp_webhook(wh: Webhook) -> dict:
    """Обновление статуса письма по событию ESP."""
    async with db.pool().acquire() as con:
        if wh.event in _EVENT_COL:
            col = _EVENT_COL[wh.event]
            await con.execute(
                f"UPDATE email_log SET {col} = now(), status = $2 WHERE id = $1",
                wh.log_id, wh.event,
            )
        elif wh.event in ("bounced", "unsubscribed", "failed"):
            await con.execute(
                "UPDATE email_log SET status = $2 WHERE id = $1", wh.log_id, wh.event
            )
            if wh.event == "unsubscribed":
                # Отписка снимает из всех сценариев немедленно (ТЗ 5).
                await con.execute(
                    """UPDATE subscribers SET is_unsubscribed = TRUE
                       WHERE user_id = (SELECT user_id FROM email_log WHERE id = $1)""",
                    wh.log_id,
                )
        else:
            return {"ok": False, "reason": "unknown event"}
    return {"ok": True}


async def run_attribution(con) -> int:
    """Атрибуция заказа к письму (ТЗ 6). last-click в окне; один заказ = одно письмо.

    Для заказа ищем ПОСЛЕДНИЙ клик того же пользователя до заказа в пределах окна;
    привязываем заказ к этому письму (если оба ещё не привязаны). Возвращает число привязок.
    """
    from app import app_settings
    window_hours = (await app_settings.get(con))["attribution_window_hours"]
    n = 0
    orders = await con.fetch(
        """SELECT o.order_id, o.user_id, o.order_date,
                  (SELECT COALESCE(SUM((i->>'price')::numeric * (i->>'qty')::int), 0)
                     FROM jsonb_array_elements(o.items) i) AS total
           FROM orders o
           WHERE lower(o.status) NOT IN ('cancelled','canceled','returned','refunded')
             AND NOT EXISTS (SELECT 1 FROM email_log e WHERE e.attributed_order_id = o.order_id)"""
    )
    for o in orders:
        log = await con.fetchrow(
            """SELECT id FROM email_log
               WHERE user_id = $1 AND clicked_at IS NOT NULL
                 AND attributed_order_id IS NULL
                 AND $2 >= clicked_at
                 AND $2 <= clicked_at + make_interval(hours => $3)
               ORDER BY clicked_at DESC LIMIT 1""",
            o["user_id"], o["order_date"], window_hours,
        )
        if log is None:
            continue
        await con.execute(
            "UPDATE email_log SET attributed_order_id = $2, revenue = $3 WHERE id = $1",
            log["id"], o["order_id"], o["total"],
        )
        n += 1
    return n


@router.get("/kpi")
async def kpi() -> dict:
    """6 метрик по каждому сервису рядом с бенчмарком + флаг просадки (ТЗ 6)."""
    async with db.pool().acquire() as con:
        rows = await con.fetch(
            """SELECT service,
                      count(*) FILTER (WHERE sent_at IS NOT NULL)      AS sent,
                      count(*) FILTER (WHERE delivered_at IS NOT NULL) AS delivered,
                      count(*) FILTER (WHERE opened_at IS NOT NULL)    AS opened,
                      count(*) FILTER (WHERE clicked_at IS NOT NULL)   AS clicked,
                      count(*) FILTER (WHERE status = 'unsubscribed')  AS unsub,
                      count(attributed_order_id)                       AS orders,
                      COALESCE(SUM(revenue), 0)                        AS revenue
               FROM email_log GROUP BY service"""
        )
    out = {}
    for r in rows:
        svc = r["service"]
        delivered = r["delivered"] or 0
        orders = r["orders"] or 0
        m = {
            "sent": r["sent"],
            "deliverability": _ratio(delivered, r["sent"]),
            "open": _ratio(r["opened"], delivered),
            "ctr": _ratio(r["clicked"], delivered),
            "conversion": _ratio(orders, delivered),
            "unsub": _ratio(r["unsub"], delivered),
            "avg_check": float(r["revenue"]) / orders if orders else None,
            "revenue": float(r["revenue"]),
            "orders": orders,
        }
        out[svc] = {"metrics": m, "benchmark": BENCHMARK.get(svc), "status": _status(svc, m)}
    return out


def _ratio(a, b):
    return round(a / b, 4) if b else None


def _status(service: str, m: dict) -> dict:
    """Флаг просадки >20-30% от бенчмарка (ТЗ 6). green/yellow/red по ключевым метрикам."""
    bench = BENCHMARK.get(service)
    if not bench:
        return {}
    flags = {}
    for key in ("deliverability", "open", "ctr", "conversion"):
        actual, target = m.get(key), bench.get(key)
        if actual is None or not target:
            flags[key] = "n/a"
            continue
        drop = (target - actual) / target
        flags[key] = "green" if drop <= 0.20 else ("yellow" if drop <= 0.30 else "red")
    return flags


def _demo() -> None:
    """Self-check формул метрик и флагов просадки."""
    assert _ratio(97, 100) == 0.97
    assert _ratio(0, 0) is None
    # В пределах бенчмарка → green; просадка 25% → yellow; 40% → red.
    m_ok = {"deliverability": 0.95, "open": 0.087, "ctr": 0.0056, "conversion": 0.0171}
    assert _status("best_offer", m_ok)["open"] == "green"
    m_yellow = {"deliverability": 0.75, "open": 0.087, "ctr": 0.0056, "conversion": 0.0171}
    assert _status("best_offer", m_yellow)["deliverability"] == "yellow"  # (0.971-0.75)/0.971≈0.228
    m_red = {"deliverability": 0.5, "open": 0.087, "ctr": 0.0056, "conversion": 0.0171}
    assert _status("best_offer", m_red)["deliverability"] == "red"
    print("analytics._demo OK")


if __name__ == "__main__":
    _demo()
