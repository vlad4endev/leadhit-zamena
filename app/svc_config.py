"""Конфиг сервисов из БД (админка ТЗ 8.1). Воркеры читают вкл/выкл и бизнес-параметры
на каждом тике — правки из админ-панели действуют без перезапуска.
"""
from __future__ import annotations

import json

# Сколько товаров максимум уезжает в одно письмо. Верхняя граница жёсткая: она же в
# CHECK на top5_by_category.position (см. db/migrations/007_items_per_email.sql), выше
# подборку просто некуда положить.
MAX_ITEMS_PER_EMAIL = 30

# Общие поля отправки (как «Параметры рассылки» в LeadHit).
_SENDER = {"sender_email": "zakaz@groster.me", "sender_name": "Магазин Groster.me", "template": "default"}

DEFAULTS = {
    # min_engagement по умолчанию 2: импортированная база начинает с тех, кто письма
    # хотя бы открывал. Своих лидов (engagement IS NULL) порог не касается, поэтому для
    # установки без импорта значение ничего не меняет.
    "best_offer": {**_SENDER, "subject": "Подборка товаров для вас",
                   "interval_days": 30, "after_purchase_days": 20, "send_hour": 9,
                   "max_per_day": 0, "min_engagement": 2,
                   "items_per_email": MAX_ITEMS_PER_EMAIL},
    "cart": {**_SENDER, "subject": "Ваша корзина ждёт",
             "cooldown_hours": 72, "depart_timeout_sec": 180, "grace_sec": 90},
    "postsale": {**_SENDER, "subject": "Спасибо за заказ — рекомендации для вас",
                 "delay_days": 7, "items_per_email": MAX_ITEMS_PER_EMAIL},
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
    "min_engagement": {"type": "number", "group": "timing",
                       "label": "Порог прогрева базы: 3 кликали, 2 открывали, 1 покупали, 0 все"},
    "delay_days": {"type": "number", "group": "timing", "label": "Задержка отправки, дней"},
    "items_per_email": {"type": "number", "group": "timing",
                        "label": f"Товаров в письме (1..{MAX_ITEMS_PER_EMAIL})"},
    "cooldown_hours": {"type": "number", "group": "timing", "label": "Частотный кап, часов"},
    "depart_timeout_sec": {"type": "number", "group": "timing", "label": "Таймаут ухода, сек"},
    "grace_sec": {"type": "number", "group": "timing", "label": "Grace-период, сек"},
}

# Правила аудитории (read-only): наш аналог «конструктора условий» LeadHit, зашитый по ТЗ.
AUDIENCE = {
    "best_offer": ["Есть email и подписка активна",
                   "Импортированный лид проходит порог прогрева (свои лиды — всегда)",
                   "Прошло ≥ интервала с последней отправки",
                   "Покупка перебивает таймер (≥ N дней от покупки)", "Нет других триггеров за 24 ч"],
    "cart": ["Корзина не пуста", "Email известен", "Заказ не оформлен", "Не чаще 1 письма / кап, часов"],
    "postsale": ["Заказ не отменён/возвращён", "Email не отписан", "1 письмо на заказ",
                 "Уступает Корзине в тот же день"],
}


def items_limit(cfg: dict) -> int:
    """Сколько товаров положить в письмо: значение из админки, зажатое в 1..MAX.
    Значение приходит из редактируемого JSON — принимаем и мусор, но не пускаем дальше."""
    try:
        n = int(cfg.get("items_per_email") or MAX_ITEMS_PER_EMAIL)
    except (TypeError, ValueError):
        n = MAX_ITEMS_PER_EMAIL
    return max(1, min(MAX_ITEMS_PER_EMAIL, n))


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


def _demo() -> None:
    """Self-check зажима «Товаров в письме» (значение правится руками в админке)."""
    assert items_limit({}) == MAX_ITEMS_PER_EMAIL                 # поле не задано
    assert items_limit({"items_per_email": 12}) == 12
    assert items_limit({"items_per_email": 999}) == MAX_ITEMS_PER_EMAIL
    assert items_limit({"items_per_email": -3}) == 1
    assert items_limit({"items_per_email": "abc"}) == MAX_ITEMS_PER_EMAIL   # мусор из JSON
    assert items_limit({"items_per_email": 0}) == MAX_ITEMS_PER_EMAIL      # 0 = «по умолчанию»
    assert DEFAULTS["best_offer"]["items_per_email"] == MAX_ITEMS_PER_EMAIL
    print("svc_config._demo OK")


if __name__ == "__main__":
    _demo()
