"""Диагностический журнал событий проекта.

Пишет в `activity_log` всё, что важно для разбора авторассылок: старты батчей,
skip на gate-проверках, сбои отправки, отмены очереди, ошибки воркеров.
Ошибки записи в журнал глотаются — диагностика не должна ломать рассылку.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app import db

# Сколько дней хранить записи (старше — удаляются при prune).
RETENTION_DAYS = 30

LEVELS = ("debug", "info", "warn", "error")
SOURCES = (
    "best_offer", "cart", "postsale", "mailer", "worker",
    "admin", "onec", "import", "system",
)


async def write(
    con=None,
    *,
    level: str = "info",
    source: str,
    event: str,
    message: str,
    service: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    order_id: Optional[str] = None,
    ref_id: Optional[int] = None,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """Записать событие. `con` — опциональное соединение; иначе берём пул."""
    lvl = level if level in LEVELS else "info"
    src = source if source in SOURCES else "system"
    payload = json.dumps(details or {}, ensure_ascii=False, default=str)
    sql = """INSERT INTO activity_log
               (level, source, event, message, service, user_id, session_id, order_id, ref_id, details)
             VALUES($1, $2, $3, $4, $5::service_kind, $6, $7, $8, $9, $10::jsonb)"""
    args = (lvl, src, event[:120], message[:2000], service, user_id, session_id, order_id, ref_id, payload)
    try:
        if con is not None:
            await con.execute(sql, *args)
        else:
            await db.pool().execute(sql, *args)
    except Exception as e:  # noqa: BLE001 — журнал не должен ронять воркер
        print(f"[activity_log] write fail: {type(e).__name__}: {e}")


async def prune(con=None, days: int = RETENTION_DAYS) -> int:
    """Удалить записи старше `days`. Возвращает число удалённых строк."""
    sql = "DELETE FROM activity_log WHERE created_at < now() - make_interval(days => $1)"
    try:
        if con is not None:
            result = await con.execute(sql, days)
        else:
            result = await db.pool().execute(sql, days)
        # asyncpg: "DELETE N"
        return int(str(result).split()[-1]) if result else 0
    except Exception as e:  # noqa: BLE001
        print(f"[activity_log] prune fail: {type(e).__name__}: {e}")
        return 0


def reason_ru(reason: str) -> str:
    """Человекочитаемые причины skip/cancel для UI и сообщений."""
    return {
        "empty_cart": "пустая корзина",
        "unknown_products": "товары не найдены в каталоге",
        "no_email": "нет email / подписчика",
        "no_consent": "нет согласия на рассылку (152-ФЗ)",
        "order_placed": "заказ уже оформлен",
        "unsubscribed": "подписчик отписан",
        "cooldown": "кап (cooldown)",
        "disabled": "сценарий выключен",
        "wrong_hour": "не час плановой отправки",
        "no_products": "нечего предложить (топ-5 / out-of-stock)",
        "oos": "все товары out-of-stock",
        "order_cancelled": "заказ отменён / не найден",
        "no_subscriber": "подписчик недоступен (нет email/согласия/отписан)",
        "antidupe": "антидубль: уже был триггер сегодня",
        "no_cross_sell": "cross-sell пуст (ТЗ 4.8)",
        "mail_failed": "ошибка отправки (mailer)",
        "max_attempts": "исчерпаны попытки отправки",
        "day_limit": "достигнут дневной лимит писем",
        "race_order": "гонка: заказ оформлен перед отправкой",
    }.get(reason, reason)
