"""Глобальные настройки приложения из БД (app_config) поверх дефолтов из .env.

Редактируемые ключи — в EDITABLE (с типом и группой для UI). Секреты (SMTP-пароль,
1С-токен) не редактируются и не отдаются наружу.
"""
from __future__ import annotations

import json

from app.config import settings

# key -> (тип, группа, подпись, «нужен перезапуск воркеров»)
# Отправитель почты настраивается в разделе «Почта» (mailer-service), не здесь — без дублей.
EDITABLE = {
    "attribution_window_hours": ("number", "attr", "Окно атрибуции, часов", False),
    "onec_sync_tick_sec": ("number", "onec", "Синхронизация каталога 1С, сек", True),
    "cart_tick_sec": ("number", "workers", "Тик корзины, сек", True),
    "postsale_tick_sec": ("number", "workers", "Тик постпродажи, сек", True),
    "attribution_tick_sec": ("number", "workers", "Тик атрибуции, сек", True),
    "best_offer_tick_sec": ("number", "workers", "Тик Best Offer, сек", True),
}


def _env_defaults() -> dict:
    return {k: getattr(settings, k) for k in EDITABLE}


async def get(con) -> dict:
    """Полный набор редактируемых настроек: значения из БД поверх дефолтов .env."""
    values = _env_defaults()
    for r in await con.fetch("SELECT key, value FROM app_config"):
        if r["key"] in EDITABLE:
            v = r["value"]
            values[r["key"]] = json.loads(v) if isinstance(v, str) else v
    return values


async def template_look(con) -> dict:
    """Оформление письма (цвет/шапка/кнопка/футер) из app_config поверх дефолтов шаблона."""
    from app.templates import LOOK_DEFAULTS
    look = dict(LOOK_DEFAULTS)
    for r in await con.fetch("SELECT key, value FROM app_config WHERE key LIKE 'tpl\\_%'"):
        k = r["key"][4:]
        if k in look:
            look[k] = json.loads(r["value"]) if isinstance(r["value"], str) else r["value"]
    return look


async def set_template_look(con, patch: dict) -> None:
    from app.templates import LOOK_DEFAULTS
    for k, v in patch.items():
        if k in LOOK_DEFAULTS and v not in (None, ""):
            await con.execute(
                """INSERT INTO app_config(key, value) VALUES($1, $2::jsonb)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                "tpl_" + k, json.dumps(str(v)))


async def active_template(con, service: str):
    """Активный шаблон сценария {id, blocks} или None (тогда воркер рендерит дефолт)."""
    row = await con.fetchrow(
        "SELECT id, blocks FROM email_templates WHERE service = $1 AND is_active", service)
    if row is None:
        return None
    blocks = row["blocks"]
    return {"id": row["id"], "blocks": json.loads(blocks) if isinstance(blocks, str) else blocks}


async def set_many(con, patch: dict) -> None:
    """Сохранить настройки. Приводим типы по EDITABLE, чужие ключи игнорируем."""
    for key, val in patch.items():
        meta = EDITABLE.get(key)
        if not meta:
            continue
        val = int(val) if meta[0] == "number" else str(val)
        await con.execute(
            """INSERT INTO app_config(key, value) VALUES($1, $2::jsonb)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            key, json.dumps(val),
        )
