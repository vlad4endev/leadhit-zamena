"""Конфиг сервисов из БД (админка ТЗ 8.1). Воркеры читают вкл/выкл и бизнес-параметры
на каждом тике — правки из админ-панели действуют без перезапуска.
"""
from __future__ import annotations

import json

# Общие поля отправки (как «Параметры рассылки» в LeadHit).
_SENDER = {"sender_email": "zakaz@groster.me", "sender_name": "Магазин Groster.me", "template": "default"}

DEFAULTS = {
    "best_offer": {**_SENDER, "subject": "Подборка товаров для вас",
                   "interval_days": 30, "after_purchase_days": 20, "send_hour": 9, "max_per_day": 0},
    "cart": {**_SENDER, "subject": "Ваша корзина ждёт",
             "cooldown_hours": 72, "depart_timeout_sec": 180, "grace_sec": 90},
    "postsale": {**_SENDER, "subject": "Спасибо за заказ — рекомендации для вас",
                 "delay_days": 7},
}

# Метаданные полей для редактора: тип и группа. Аудитория — read-only (правила зашиты по ТЗ).
FIELD_META = {
    "sender_email": {"type": "text", "group": "sender", "label": "Адрес отправителя"},
    "sender_name": {"type": "text", "group": "sender", "label": "Имя отправителя"},
    "subject": {"type": "text", "group": "sender", "label": "Тема письма"},
    "template": {"type": "template", "group": "sender", "label": "Шаблон письма"},
    "interval_days": {"type": "number", "group": "timing", "label": "Интервал рассылки, дней"},
    "after_purchase_days": {"type": "number", "group": "timing", "label": "После покупки, дней"},
    "send_hour": {"type": "number", "group": "timing", "label": "Час отправки"},
    "max_per_day": {"type": "number", "group": "timing", "label": "Макс. писем/день (0 = без лимита)"},
    "delay_days": {"type": "number", "group": "timing", "label": "Задержка отправки, дней"},
    "cooldown_hours": {"type": "number", "group": "timing", "label": "Частотный кап, часов"},
    "depart_timeout_sec": {"type": "number", "group": "timing", "label": "Таймаут ухода, сек"},
    "grace_sec": {"type": "number", "group": "timing", "label": "Grace-период, сек"},
}

# Правила аудитории (read-only): наш аналог «конструктора условий» LeadHit, зашитый по ТЗ.
AUDIENCE = {
    "best_offer": ["Есть email и подписка активна", "Прошло ≥ интервала с последней отправки",
                   "Покупка перебивает таймер (≥ N дней от покупки)", "Нет других триггеров за 24 ч"],
    "cart": ["Корзина не пуста", "Email известен", "Заказ не оформлен", "Не чаще 1 письма / кап, часов"],
    "postsale": ["Заказ не отменён/возвращён", "Email не отписан", "1 письмо на заказ",
                 "Уступает Корзине в тот же день"],
}


async def load(con, service: str) -> dict:
    """Возвращает {'enabled': bool, ...params}. Отсутствующие параметры — из DEFAULTS."""
    row = await con.fetchrow(
        "SELECT enabled, params FROM service_config WHERE service = $1", service
    )
    params = dict(DEFAULTS.get(service, {}))
    if row:
        params.update(json.loads(row["params"]) if isinstance(row["params"], str) else row["params"])
        return {"enabled": row["enabled"], **params}
    return {"enabled": True, **params}
