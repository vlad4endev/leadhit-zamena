"""Сервис 1 — Best Offer (ТЗ раздел 2).

Плановая рассылка топ-5 по категориям с ротацией. Батч раз в сутки: выборка по условию
30/20 дней → подбор ротацией с дедупом → отправка → сдвиг указателя ПОСЛЕ отправки.
Без ML: единственный признак персонализации — категория.
"""
from __future__ import annotations

from app import app_settings, svc_config
from app.mailer import get_mailer
from app.templates import DEFAULT_BLOCKS, render_blocks, render_email

DEDUP_LAST_N = 3  # дедуп по товарам из последних N писем Best Offer (ТЗ 2.4)


def rotate_and_pick(
    start: str | None,
    order: list[str],
    top5: dict[str, list[str]],
    recent: set[str],
    min_items: int = 2,
) -> tuple[str | None, list[str], str | None]:
    """Ротация категорий с дедупом (ТЗ 2.4).

    Идём по категориям от start циклично. Первая, где после исключения recent осталось
    >= min_items товаров, — берётся. Возвращает (категория, товары, СЛЕДУЮЩИЙ указатель).
    Если ни одна не набирает min_items — фолбэк: первая категория с любыми товарами
    (ослабленный дедуп), чтобы не уйти в пустоту у «выжженного» юзера.
    """
    if not order:
        return None, [], start
    n = len(order)
    idx0 = order.index(start) if start in order else 0

    for k in range(n):
        cat = order[(idx0 + k) % n]
        picked = [p for p in top5.get(cat, []) if p not in recent][:5]
        if len(picked) >= min_items:
            return cat, picked, order[(idx0 + k + 1) % n]

    # Фолбэк: весь фид «выжжен» дедупом — берём лучшее доступное.
    for k in range(n):
        cat = order[(idx0 + k) % n]
        picked = top5.get(cat, [])[:5]
        if picked:
            return cat, picked, order[(idx0 + k + 1) % n]
    return None, [], start


async def _categories_order(con) -> list[str]:
    rows = await con.fetch("SELECT category_id FROM categories ORDER BY sort_order")
    return [r["category_id"] for r in rows]


async def _top5_map(con) -> dict[str, list[str]]:
    rows = await con.fetch(
        "SELECT category_id, product_id FROM top5_by_category ORDER BY category_id, position"
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["category_id"], []).append(r["product_id"])
    return out


async def _recent_products(con, user_id: str) -> set[str]:
    rows = await con.fetch(
        """SELECT product_ids FROM email_log
           WHERE user_id = $1 AND service = 'best_offer'
           ORDER BY created_at DESC LIMIT $2""",
        user_id, DEDUP_LAST_N,
    )
    recent: set[str] = set()
    for r in rows:
        recent.update(r["product_ids"])
    return recent


async def _candidates(con, interval_days: int, after_purchase_days: int):
    """Выборка по условию 30/20 дней + фильтры (ТЗ 2.2). Покупка перебивает 30-дневный таймер."""
    return await con.fetch(
        """SELECT user_id, email, rotation_pointer_category_id, last_purchase_category_id
           FROM subscribers
           WHERE email IS NOT NULL AND NOT is_unsubscribed
             AND (last_any_trigger_at IS NULL
                  OR last_any_trigger_at < now() - interval '24 hours')     -- антидубль
             AND (
                 last_sent_best_offer_at IS NULL                            -- первое письмо
                 OR (last_purchase_at IS NOT NULL
                     AND last_purchase_at > last_sent_best_offer_at
                     AND last_purchase_at + make_interval(days => $2) <= now())  -- 20д от покупки
                 OR ((last_purchase_at IS NULL OR last_purchase_at <= last_sent_best_offer_at)
                     AND last_sent_best_offer_at + make_interval(days => $1) <= now())  -- 30д от отправки
             )""",
        interval_days, after_purchase_days,
    )


async def _load_products(con, product_ids: list[str]) -> list[dict]:
    rows = await con.fetch(
        """SELECT product_id, name, price, image_url, product_url FROM products
           WHERE product_id = ANY($1::text[]) AND in_stock""",
        product_ids,
    )
    by_id = {r["product_id"]: dict(r) for r in rows}
    return [dict(by_id[pid], price=float(by_id[pid]["price"])) for pid in product_ids if pid in by_id]


async def run_batch(con, mailer=None, force: bool = False) -> int:
    """Батч-джоб Best Offer (раз/сутки). Возвращает число отправленных писем."""
    cfg = await svc_config.load(con, "best_offer")
    if not force and not cfg["enabled"]:
        return 0
    # Час отправки: плановый батч уходит только в заданный час (ручной запуск — в обход).
    if not force and int(cfg.get("send_hour", 9)) != (await con.fetchval("SELECT extract(hour from now())")):
        return 0
    max_per_day = int(cfg.get("max_per_day", 0))
    await app_settings.load_site(con)   # адреса из админки: ссылка отписки и CTA в магазин
    look = await app_settings.template_look(con)
    tpl = await app_settings.active_template(con, "best_offer")
    blocks = tpl["blocks"] if tpl else DEFAULT_BLOCKS.get("best_offer")
    template_id = tpl["id"] if tpl else None
    mailer = mailer or get_mailer()
    order = await _categories_order(con)
    top5 = await _top5_map(con)
    default_start = order[0] if order else None
    sent = 0

    for cand in await _candidates(con, cfg["interval_days"], cfg["after_purchase_days"]):
        if max_per_day and sent >= max_per_day:
            break  # дневной лимит писем (как «Макс. кол-во писем в день» в LeadHit)
        start = cand["rotation_pointer_category_id"] or cand["last_purchase_category_id"] or default_start
        recent = await _recent_products(con, cand["user_id"])
        category, product_ids, next_ptr = rotate_and_pick(start, order, top5, recent)
        if not product_ids:
            continue  # нечего предложить

        products = await _load_products(con, product_ids)
        if blocks:
            html = render_blocks(blocks, products, cand["user_id"], "best_offer", look)
        else:
            intro = "<h2>Подборка для вас</h2>"
            html = render_email(intro, products, cand["user_id"], "best_offer", cfg.get("template", "default"), look)
        # Строку лога создаём ДО отправки (log_id связывает события доставки).
        log_id = await con.fetchval(
            """INSERT INTO email_log(user_id, service, category_id, product_ids, template_id, status)
               VALUES($1, 'best_offer', $2, $3, $4, 'queued') RETURNING id""",
            cand["user_id"], category, product_ids, template_id,
        )
        ok = await mailer.send(cand["email"], cfg["subject"], html,
                               cfg["sender_email"], cfg["sender_name"], meta={"log_id": log_id})
        if not ok:
            await con.execute("UPDATE email_log SET status='failed' WHERE id=$1", log_id)
            continue

        async with con.transaction():
            await con.execute("UPDATE email_log SET status='sent', sent_at=now() WHERE id=$1", log_id)
            # Указатель двигается ТОЛЬКО после успешной отправки (ТЗ 2.4).
            await con.execute(
                """UPDATE subscribers
                   SET last_sent_best_offer_at = now(), last_any_trigger_at = now(),
                       rotation_pointer_category_id = $2
                   WHERE user_id = $1""",
                cand["user_id"], next_ptr,
            )
        sent += 1
    return sent


def _demo() -> None:
    """Self-check ротации+дедупа (ТЗ 2.4)."""
    order = ["shoes", "bags", "acc"]
    top5 = {"shoes": ["s1", "s2", "s3"], "bags": ["b1", "b2"], "acc": ["a1"]}

    # Старт shoes, дедупа нет → shoes, next=bags.
    assert rotate_and_pick("shoes", order, top5, set()) == ("shoes", ["s1", "s2", "s3"], "bags")
    # shoes выжжена дедупом (осталось <2) → переходим на bags, next=acc.
    assert rotate_and_pick("shoes", order, top5, {"s2", "s3"}) == ("bags", ["b1", "b2"], "acc")
    # acc имеет 1 товар (<2) → пропускаем, цикл к shoes.
    assert rotate_and_pick("acc", order, top5, set()) == ("shoes", ["s1", "s2", "s3"], "bags")
    # Всё выжжено → фолбэк на первую непустую категорию (ослабленный дедуп).
    cat, ids, nxt = rotate_and_pick("shoes", order, {"shoes": ["s1"], "bags": [], "acc": []},
                                    {"s1"})
    assert cat == "shoes" and ids == ["s1"] and nxt == "bags", (cat, ids, nxt)
    # start вне списка → начинаем с первой категории.
    assert rotate_and_pick(None, order, top5, set())[0] == "shoes"
    print("best_offer._demo OK")


if __name__ == "__main__":
    _demo()
