"""Сервис 1 — Best Offer (ТЗ раздел 2).

Плановая рассылка топ-5 по категориям с ротацией. Батч раз в сутки: выборка по условию
30/20 дней → подбор ротацией с дедупом → отправка → сдвиг указателя ПОСЛЕ отправки.
Без ML: единственный признак персонализации — категория.
"""
from __future__ import annotations

from app import activity_log, app_settings, images, svc_config
from app.mailer import get_mailer
from app.templates import DEFAULT_BLOCKS, render_blocks, render_email

DEDUP_LAST_N = 3  # дедуп по товарам из последних N писем Best Offer (ТЗ 2.4)


def rotate_and_pick(
    start: str | None,
    order: list[str],
    top5: dict[str, list[str]],
    recent: set[str],
    min_items: int = 2,
    limit: int = 30,
) -> tuple[str | None, list[str], str | None]:
    """Ротация категорий с дедупом (ТЗ 2.4).

    Идём по категориям от start циклично. Первая, где после исключения recent осталось
    >= min_items товаров, — основная. Дальше добираем уникальные id из следующих
    категорий до `limit` (по умолчанию 30). Указатель сдвигается на категорию
    ПОСЛЕ основной. Если ни одна не набирает min_items — фолбэк без дедупа.
    """
    if not order:
        return None, [], start
    limit = max(1, int(limit or 30))
    n = len(order)
    idx0 = order.index(start) if start in order else 0

    primary_k: int | None = None
    for k in range(n):
        cat = order[(idx0 + k) % n]
        avail = [p for p in top5.get(cat, []) if p not in recent]
        if len(avail) >= min_items:
            primary_k = k
            break

    strict_dedup = primary_k is not None
    if primary_k is None:
        # Фолбэк: весь фид «выжжен» дедупом — берём первую непустую без фильтра recent.
        for k in range(n):
            cat = order[(idx0 + k) % n]
            if top5.get(cat, []):
                primary_k = k
                break
    if primary_k is None:
        return None, [], start

    primary = order[(idx0 + primary_k) % n]
    picked: list[str] = []
    seen: set[str] = set()
    for k in range(n):
        cat = order[(idx0 + primary_k + k) % n]
        for pid in top5.get(cat, []):
            if pid in seen:
                continue
            if strict_dedup and pid in recent:
                continue
            picked.append(pid)
            seen.add(pid)
            if len(picked) >= limit:
                break
        if len(picked) >= limit:
            break
    next_ptr = order[(idx0 + primary_k + 1) % n]
    return primary, picked, next_ptr


async def _categories_order(con) -> list[str]:
    rows = await con.fetch("SELECT category_id FROM categories ORDER BY sort_order")
    return [r["category_id"] for r in rows]


async def _top5_map(con, per_cat: int = 30,
                    categories: list[str] | None = None) -> dict[str, list[str]]:
    """Пул по категориям: топ-5 фида + добор из каталога (in_stock + фото), до per_cat."""
    per_cat = max(1, int(per_cat or 30))
    # Только in_stock: иначе категория с «нет в наличии» проходит порог min_items
    # и в письмо уходит мало карточек, хотя следующая категория набирает полную подборку.
    rows = await con.fetch(
        """SELECT t.category_id, t.product_id
           FROM top5_by_category t
           JOIN products p ON p.product_id = t.product_id AND p.in_stock
           ORDER BY t.category_id, t.position"""
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["category_id"], []).append(r["product_id"])
    # Добор из каталога — топ фида ≤5, а в письме нужно до 30 уникальных.
    # Берём все категории ротации, не только те, что уже есть в топе.
    cats = [c for c in (categories or list(out.keys())) if c]
    need = [c for c in cats if len(out.get(c, [])) < per_cat]
    if need:
        extra = await con.fetch(
            f"""SELECT category_id, product_id FROM products
               WHERE category_id = ANY($1::text[]) AND in_stock
                 AND {images.HAS_PHOTO_SQL}
               ORDER BY category_id, updated_at DESC NULLS LAST, product_id""",
            need,
        )
        for r in extra:
            bucket = out.setdefault(r["category_id"], [])
            if r["product_id"] in bucket or len(bucket) >= per_cat:
                continue
            bucket.append(r["product_id"])
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
             AND consent_at IS NOT NULL                                   -- 152-ФЗ: есть согласие
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
    ids: list[str] = []
    seen: set[str] = set()
    for raw in product_ids:
        pid = str(raw or "").strip()
        if pid and pid not in seen:
            seen.add(pid)
            ids.append(pid)
    if not ids:
        return []
    rows = await con.fetch(
        f"""SELECT product_id, name, price, image_url, product_url FROM products
           WHERE product_id = ANY($1::text[]) AND in_stock
             AND {images.HAS_PHOTO_SQL}""",
        ids,
    )
    by_id = {r["product_id"]: dict(r) for r in rows}
    # Порядок топ-5, без повторов. in_stock — второй барьер, если остаток кончился между запросами.
    return [dict(by_id[pid], price=float(by_id[pid]["price"])) for pid in ids if pid in by_id]


async def _fallback_catalog_products(con, recent: set[str], limit: int = 30) -> list[dict]:
    """Запасная подборка: любые in_stock с фото из каталога, минус недавний дедуп.
    Нужна, когда топ категории выжжен по фото/OOS, а письмо иначе уйдёт пустым."""
    limit = max(1, int(limit or 30))
    rows = await con.fetch(
        f"""SELECT product_id, name, price, image_url, product_url FROM products
           WHERE in_stock AND {images.HAS_PHOTO_SQL}
           ORDER BY updated_at DESC NULLS LAST, product_id
           LIMIT $1""",
        max(limit * 4, 40),
    )
    picked = []
    seen: set[str] = set()
    for r in rows:
        pid = r["product_id"]
        if pid in recent or pid in seen:
            continue
        picked.append(dict(r, price=float(r["price"])))
        seen.add(pid)
        if len(picked) >= limit:
            break
    if len(picked) < limit:
        # Дедуп выжег всё — берём как есть, без повторов.
        for r in rows:
            pid = r["product_id"]
            if pid in seen:
                continue
            picked.append(dict(r, price=float(r["price"])))
            seen.add(pid)
            if len(picked) >= limit:
                break
    return await images.warm_products(picked, require_live=True)

async def run_batch(con, mailer=None, force: bool = False) -> int:
    """Батч-джоб Best Offer (раз/сутки). Возвращает число отправленных писем."""
    cfg = await svc_config.load(con, "best_offer")
    if not force and not cfg["enabled"]:
        return 0  # штатный idle — не пишем в журнал каждый тик
    # Час отправки: плановый батч уходит только в заданный час (ручной запуск — в обход).
    hour_now = int(await con.fetchval("SELECT extract(hour from now())"))
    send_hour = int(cfg.get("send_hour", 9))
    if not force and send_hour != hour_now:
        return 0  # штатный idle до send_hour — без шума в журнале
    max_per_day = int(cfg.get("max_per_day", 0))
    await app_settings.load_site(con)   # адреса из админки: ссылка отписки и CTA в магазин
    look = await app_settings.template_look(con)
    tpl = await app_settings.active_template(con, "best_offer")
    blocks = tpl["blocks"] if tpl else DEFAULT_BLOCKS.get("best_offer")
    template_id = tpl["id"] if tpl else None
    mailer = mailer or get_mailer()
    items_limit = max(1, min(int(cfg.get("items_limit") or 30), 60))
    order = await _categories_order(con)
    top5 = await _top5_map(con, per_cat=items_limit, categories=order)
    default_start = order[0] if order else None
    candidates = await _candidates(con, cfg["interval_days"], cfg["after_purchase_days"])
    sent = 0
    skipped = 0

    await activity_log.write(
        con, level="info", source="best_offer", event="batch_start", service="best_offer",
        message=f"старт батча Best Offer: кандидатов {len(candidates)}"
                + (f", лимит {max_per_day}/день" if max_per_day else "")
                + (" (ручной запуск)" if force else ""),
        details={"candidates": len(candidates), "max_per_day": max_per_day, "force": force,
                 "template_id": template_id, "categories": len(order),
                 "items_limit": items_limit},
    )

    for cand in candidates:
        if max_per_day and sent >= max_per_day:
            await activity_log.write(
                con, level="warn", source="best_offer", event="skip", service="best_offer",
                message=f"остановка батча: {activity_log.reason_ru('day_limit')} ({max_per_day})",
                details={"reason": "day_limit", "sent": sent, "max_per_day": max_per_day},
            )
            break  # дневной лимит писем (как «Макс. кол-во писем в день» в LeadHit)
        try:
            n = await _send_one(
                con, cand, mailer, cfg, look, blocks, template_id, order, top5, default_start,
                items_limit)
            if n:
                sent += n
            else:
                skipped += 1
        except Exception as e:  # noqa: BLE001 — один битый профиль не останавливает батч
            skipped += 1
            print(f"[best_offer] user {cand['user_id']} ERROR {type(e).__name__}: {e}")
            await activity_log.write(
                con, level="error", source="best_offer", event="send_failed", service="best_offer",
                user_id=cand["user_id"],
                message=f"ошибка профиля: {type(e).__name__}: {e}",
                details={"error": str(e)[:500]},
            )

    await activity_log.write(
        con, level="info", source="best_offer", event="batch_done", service="best_offer",
        message=f"батч завершён: отправлено {sent}, пропущено {skipped}, кандидатов {len(candidates)}",
        details={"sent": sent, "skipped": skipped, "candidates": len(candidates)},
    )
    return sent


async def _send_one(con, cand, mailer, cfg, look, blocks, template_id, order, top5, default_start,
                    items_limit: int = 30) -> int:
    limit = max(1, min(int(items_limit or 30), 60))
    start = cand["rotation_pointer_category_id"] or cand["last_purchase_category_id"] or default_start
    recent = await _recent_products(con, cand["user_id"])
    category, product_ids, next_ptr = rotate_and_pick(start, order, top5, recent, limit=limit)
    products: list[dict] = []
    if product_ids:
        # Только с фото + прогрев CDN (в письме — абсолютный static URL, не /img/ редирект).
        products = images.photo_first(await _load_products(con, product_ids), limit)
        products = await images.warm_products(products, require_live=True)
    if not products:
        # Топ пуст / выжжен по фото — не бросаем письмо: любые in_stock с фото из каталога.
        products = await _fallback_catalog_products(con, recent, limit=limit)
        category = category or "catalog"
        if not next_ptr and order:
            next_ptr = order[(order.index(start) + 1) % len(order)] if start in order else order[0]
    if not products:
        await activity_log.write(
            con, level="warn", source="best_offer", event="skip", service="best_offer",
            user_id=cand["user_id"],
            message=f"skip: {activity_log.reason_ru('no_products')}",
            details={"reason": "no_products", "start_category": start, "recent_n": len(recent),
                     "picked": product_ids},
        )
        return 0
    if blocks:
        html = render_blocks(blocks, products, cand["user_id"], "best_offer", look)
    else:
        intro = "<h2>Подборка для вас</h2>"
        html = render_email(intro, products, cand["user_id"], "best_offer", cfg.get("template", "default"), look)
    sent_ids = [p["product_id"] for p in products]
    log_id = await con.fetchval(
        """INSERT INTO email_log(user_id, service, category_id, product_ids, template_id,
                                subject, html, status)
           VALUES($1, 'best_offer', $2, $3, $4, $5, $6, 'queued') RETURNING id""",
        cand["user_id"], category, sent_ids, template_id, cfg["subject"], html,
    )
    ok = await mailer.send(cand["email"], cfg["subject"], html,
                           cfg["sender_email"], cfg["sender_name"], meta={"log_id": log_id})
    if not ok:
        await con.execute("UPDATE email_log SET status='failed' WHERE id=$1", log_id)
        await activity_log.write(
            con, level="error", source="best_offer", event="send_failed", service="best_offer",
            user_id=cand["user_id"], ref_id=log_id,
            message=f"отправка не удалась: {activity_log.reason_ru('mail_failed')}",
            details={"reason": "mail_failed", "category": category, "product_ids": sent_ids,
                     "to": cand["email"]},
        )
        return 0

    async with con.transaction():
        await con.execute("UPDATE email_log SET status='sent', sent_at=now() WHERE id=$1", log_id)
        await con.execute(
            """UPDATE subscribers
               SET last_sent_best_offer_at = now(), last_any_trigger_at = now(),
                   rotation_pointer_category_id = $2
               WHERE user_id = $1""",
            cand["user_id"], next_ptr,
        )
    await activity_log.write(
        con, level="info", source="best_offer", event="queued", service="best_offer",
        user_id=cand["user_id"], ref_id=log_id,
        message=f"принято в очередь отправки: категория {category}, товаров {len(sent_ids)} "
                f"→ {cand['email']}",
        details={"category": category, "product_ids": sent_ids, "next_pointer": next_ptr,
                 "to": cand["email"]},
    )
    return 1


def _demo() -> None:
    """Self-check ротации+дедупа (ТЗ 2.4)."""
    order = ["shoes", "bags", "acc"]
    top5 = {"shoes": ["s1", "s2", "s3"], "bags": ["b1", "b2"], "acc": ["a1"]}

    # Старт shoes, дедупа нет → shoes + добор из следующих до limit, next=bags.
    cat, ids, nxt = rotate_and_pick("shoes", order, top5, set(), limit=30)
    assert cat == "shoes" and ids == ["s1", "s2", "s3", "b1", "b2", "a1"] and nxt == "bags", (cat, ids, nxt)
    # limit=3 → только первые 3 из основной категории.
    assert rotate_and_pick("shoes", order, top5, set(), limit=3) == ("shoes", ["s1", "s2", "s3"], "bags")
    # shoes выжжена дедупом (осталось <2) → bags + добор (без recent), next=acc.
    cat, ids, nxt = rotate_and_pick("shoes", order, top5, {"s2", "s3"}, limit=30)
    assert cat == "bags" and ids == ["b1", "b2", "a1", "s1"] and nxt == "acc", (cat, ids, nxt)
    # acc имеет 1 товар (<2) → пропускаем, цикл к shoes.
    cat, ids, nxt = rotate_and_pick("acc", order, top5, set(), limit=3)
    assert cat == "shoes" and ids == ["s1", "s2", "s3"] and nxt == "bags", (cat, ids, nxt)
    # Всё выжжено → фолбэк на первую непустую категорию (ослабленный дедуп).
    cat, ids, nxt = rotate_and_pick("shoes", order, {"shoes": ["s1"], "bags": [], "acc": []},
                                    {"s1"})
    assert cat == "shoes" and ids == ["s1"] and nxt == "bags", (cat, ids, nxt)
    # start вне списка → начинаем с первой категории.
    assert rotate_and_pick(None, order, top5, set(), limit=3)[0] == "shoes"
    # Уникальность при доборе до 30.
    big = {"c1": [f"a{i}" for i in range(20)], "c2": [f"b{i}" for i in range(20)]}
    cat, ids, nxt = rotate_and_pick("c1", ["c1", "c2"], big, set(), limit=30)
    assert cat == "c1" and len(ids) == 30 and len(set(ids)) == 30 and nxt == "c2", (cat, len(ids), nxt)
    print("best_offer._demo OK")


if __name__ == "__main__":
    _demo()
