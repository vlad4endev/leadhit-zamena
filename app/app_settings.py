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


# ── Колесо фортуны: конфиг виджета (призы + оформление) одним ключом в app_config ──
# Зеркалит DEFAULT_PRIZES в wheel.js — там это офлайн-фолбэк, здесь редактируемый источник.
WHEEL_DEFAULTS = {
    "enabled": True,
    "title": 'Колесо фортуны — <span class="acc">скидка до 50%</span>',
    "subtitle": "Оставьте email, крутаните колесо и забирайте персональный промокод.",
    "once_days": 7,        # не показывать попап чаще раза в N дней (0 — каждый заход)
    "auto_delay_sec": 12,  # задержка авто-открытия попапа на сайте
    "prizes": [
        {"label": "Скидка 5%",   "code": "GROSTER5",  "weight": 30, "color": "#bc39e5"},
        {"label": "Скидка 10%",  "code": "GROSTER10", "weight": 22, "color": "#fecc00"},
        {"label": "Промокод 7%", "code": "LUCKY7",    "weight": 20, "color": "#35cc00"},
        {"label": "Скидка 15%",  "code": "GROSTER15", "weight": 12, "color": "#fc6631"},
        {"label": "Подарок 🎁",  "code": "GIFTBOX",   "weight": 8,  "color": "#bc39e5"},
        {"label": "Скидка 25%",  "code": "GROSTER25", "weight": 5,  "color": "#fecc00"},
        {"label": "Ещё разок",   "code": "",          "weight": 2,  "color": "#35cc00", "respin": True},
        {"label": "Скидка 50%",  "code": "JACKPOT50", "weight": 1,  "color": "#fc6631"},
    ],
}


async def wheel_config(con) -> dict:
    """Конфиг колеса из app_config (ключ 'wheel') поверх дефолтов."""
    raw = await con.fetchval("SELECT value FROM app_config WHERE key = 'wheel'")
    if raw is None:
        return dict(WHEEL_DEFAULTS)
    data = json.loads(raw) if isinstance(raw, str) else raw
    return {**WHEEL_DEFAULTS, **data}


async def set_wheel_config(con, cfg: dict) -> None:
    await con.execute(
        """INSERT INTO app_config(key, value) VALUES('wheel', $1::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        json.dumps(cfg))


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
