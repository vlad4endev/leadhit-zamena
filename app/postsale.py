"""Сервис 3 — Постпродажа (ТЗ раздел 4).

Отложенное письмо +7 дней после заказа. Триггер ставит задачу в очередь при приёме
заказа; воркер разбирает задачи, у которых наступил срок, с проверками НА МОМЕНТ ОТПРАВКИ.
"""
from __future__ import annotations

import json

import asyncpg

from app import app_settings, svc_config
from app.mailer import get_mailer
from app.templates import DEFAULT_BLOCKS, render_blocks, render_email

CANCELLED_STATUSES = {"cancelled", "canceled", "returned", "refunded"}
MAX_ATTEMPTS = 5  # после N сбоев ESP/mailer задача → failed (не крутим вечно)


def normalize_order_items(items: list[dict], catalog: dict[str, dict]) -> list[dict]:
    """Позиции заказа → {product_id, category_id, price}.

    Заказ с витрины (POST /order) не несёт category_id — без него cross-sell падал
    и товары в письмо постпродажи не попадали. Категорию и цену добираем из каталога.
    Явная category_id заказа важнее каталога: это категория покупки, не текущая карточки.
    """
    out: list[dict] = []
    for i in items or []:
        pid = str(i.get("product_id") or "").strip()
        if not pid:
            continue
        row = catalog.get(pid) or {}
        cat = str(i.get("category_id") or "").strip() or str(row.get("category_id") or "").strip()
        if not cat:
            continue
        price = i.get("price")
        if price is None or price == "":
            price = row.get("price", 0)
        try:
            price_f = float(price or 0)
        except (TypeError, ValueError):
            price_f = 0.0
        out.append({"product_id": pid, "category_id": cat, "price": price_f})
    return out


def pick_cross_sell(items: list[dict], top5_by_cat: dict[str, list[str]]) -> tuple[str | None, list[str]]:
    """Подбор cross-sell (ТЗ 4.4), без ML.

    Категория самого дорогого товара → топ-5 этой категории минус уже купленное.
    Пусто → фолбэк на категорию следующего по цене товара. Возвращает (category_id, product_ids)
    или (None, []) если подобрать нечего (тогда письмо НЕ шлём — критерий 4.8).
    """
    bought = {i["product_id"] for i in items}
    # Категории заказа по убыванию цены товара (для основной категории и фолбэка).
    cats_by_price = [i["category_id"] for i in sorted(items, key=lambda i: -i["price"])]
    seen: set[str] = set()
    for cat in cats_by_price:
        if cat in seen:
            continue
        seen.add(cat)
        picked = [p for p in top5_by_cat.get(cat, []) if p not in bought]
        if picked:
            return cat, picked
    return None, []


async def enqueue_for_orders(con: asyncpg.Connection, orders: list) -> None:
    """Ставит отложенную задачу Постпродажи на +N дней. Идемпотентно (уник-индекс по order_id).

    Отменённые/возвращённые заказы задачу не порождают.
    """
    cfg = await svc_config.load(con, "postsale")
    rows = [
        (o.order_id, o.user_id, o.order_date, cfg["delay_days"])
        for o in orders
        if o.status.lower() not in CANCELLED_STATUSES
    ]
    if not rows:
        return
    # run_after в прошлом (историч. заказ при бэкофилле) → задачу не ставим: иначе на первом
    # тике Постпродажа уйдёт залпом по всей истории. Шлём только для заказов с будущим сроком.
    await con.executemany(
        """INSERT INTO send_queue(user_id, service, order_id, run_after, state)
           SELECT $2, 'postsale', $1,
                  ($3::text::timestamptz + make_interval(days => $4)), 'scheduled'
           WHERE ($3::text::timestamptz + make_interval(days => $4)) > now()
           ON CONFLICT (order_id) WHERE service = 'postsale' AND order_id IS NOT NULL
           DO NOTHING""",
        rows,
    )


async def run_due(con: asyncpg.Connection, mailer=None, force: bool = False) -> int:
    """Обрабатывает задачи Постпродажи, у которых наступил срок. Возвращает число отправленных."""
    cfg = await svc_config.load(con, "postsale")
    if not force and not cfg["enabled"]:
        return 0
    mailer = mailer or get_mailer()
    await app_settings.load_site(con)   # адреса из админки: ссылка отписки и CTA в магазин
    look = await app_settings.template_look(con)
    tpl = await app_settings.active_template(con, "postsale")
    blocks = tpl["blocks"] if tpl else DEFAULT_BLOCKS.get("postsale")
    template_id = tpl["id"] if tpl else None
    sent = 0
    jobs = await con.fetch(
        """SELECT id, user_id, order_id, attempts FROM send_queue
           WHERE service = 'postsale' AND state = 'scheduled' AND run_after <= now()
           FOR UPDATE SKIP LOCKED"""
    )
    for job in jobs:
        try:
            result = await _process_one(con, job, mailer, cfg, look, blocks, template_id)
        except Exception as e:  # noqa: BLE001 — один битый заказ не останавливает очередь
            print(f"[postsale] job {job['id']} ERROR {type(e).__name__}: {e}")
            if await _bump_attempt(con, job):
                await _finish(con, job["id"], "failed")
            continue
        if result:
            sent += 1
    return sent


async def _bump_attempt(con: asyncpg.Connection, job) -> bool:
    """+1 к attempts. True = лимит исчерпан, задачу надо закрыть как failed."""
    attempts = int(job["attempts"] or 0) + 1
    await con.execute(
        "UPDATE send_queue SET attempts = $2 WHERE id = $1", job["id"], attempts)
    return attempts >= MAX_ATTEMPTS


async def _process_one(con: asyncpg.Connection, job, mailer, cfg, look, blocks=None, template_id=None) -> bool:
    if int(job["attempts"] or 0) >= MAX_ATTEMPTS:
        await _finish(con, job["id"], "failed")
        return False

    order = await con.fetchrow(
        "SELECT order_id, user_id, email, status, items FROM orders WHERE order_id = $1",
        job["order_id"],
    )
    sub = await con.fetchrow(
        """SELECT user_id, email, is_unsubscribed, consent_at, last_any_trigger_at
           FROM subscribers WHERE user_id = $1""",
        job["user_id"],
    )

    # Проверки актуальности НА МОМЕНТ ОТПРАВКИ (ТЗ 4.5), не постановки.
    if order is None or order["status"].lower() in CANCELLED_STATUSES:
        await _finish(con, job["id"], "cancelled")
        return False
    if sub is None or sub["is_unsubscribed"] or not sub["email"] or sub["consent_at"] is None:
        await _finish(con, job["id"], "cancelled")
        return False
    # Антидубль: не более одного триггера в день (Постпродажа уступает Корзине).
    if sub["last_any_trigger_at"] is not None:
        already = await con.fetchval(
            "SELECT (now()::date = $1::date)", sub["last_any_trigger_at"]
        )
        if already:
            # Сдвигаем задачу на следующий день, а не шлём вторым письмом.
            await con.execute(
                "UPDATE send_queue SET run_after = (now() + interval '1 day') WHERE id = $1",
                job["id"],
            )
            return False

    items = await _order_items(con, order["items"])
    top5 = await _top5_map(con, [i["category_id"] for i in items])
    category, product_ids = pick_cross_sell(items, top5)
    if not product_ids:
        await _finish(con, job["id"], "cancelled")  # блок пуст → не шлём (ТЗ 4.8)
        return False

    products = await _load_products(con, product_ids)
    if not products:
        # Cross-sell выжжен out-of-stock → пустое письмо не шлём (ТЗ 4.8).
        await _finish(con, job["id"], "cancelled")
        return False
    if blocks:
        html = render_blocks(blocks, products, order["user_id"], "postsale", look)
    else:
        intro = "<h2>Спасибо за покупку!</h2><p>Возможно, вам подойдёт:</p>"
        html = render_email(intro, products, order["user_id"], "postsale", cfg.get("template", "default"), look)
    sent_ids = [p["product_id"] for p in products]
    # Строку лога создаём ДО отправки (уник-индекс по order_id держит «1 письмо на заказ»).
    log_id = await con.fetchval(
        """INSERT INTO email_log(user_id, service, category_id, product_ids, order_id, template_id, status)
           VALUES($1, 'postsale', $2, $3, $4, $5, 'queued') RETURNING id""",
        order["user_id"], category, sent_ids, order["order_id"], template_id,
    )
    ok = await mailer.send(sub["email"], cfg["subject"], html,
                           cfg["sender_email"], cfg["sender_name"], meta={"log_id": log_id})
    if not ok:
        # Удаляем queued-строку, чтобы ретрай задачи не упёрся в уник-индекс по заказу.
        await con.execute("DELETE FROM email_log WHERE id = $1", log_id)
        if await _bump_attempt(con, job):
            await _finish(con, job["id"], "failed")
        return False

    async with con.transaction():
        await con.execute("UPDATE email_log SET status='sent', sent_at=now() WHERE id=$1", log_id)
        await con.execute(
            "UPDATE subscribers SET last_sent_postsale_at = now(), last_any_trigger_at = now() WHERE user_id = $1",
            order["user_id"],
        )
        await _finish(con, job["id"], "sent")
    return True


async def _finish(con: asyncpg.Connection, job_id: int, state: str) -> None:
    await con.execute("UPDATE send_queue SET state = $2 WHERE id = $1", job_id, state)


def _parse_items(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = json.loads(raw)
    return list(raw) if isinstance(raw, list) else []


async def _order_items(con: asyncpg.Connection, raw) -> list[dict]:
    items = _parse_items(raw)
    pids = list({str(i.get("product_id") or "").strip() for i in items if str(i.get("product_id") or "").strip()})
    catalog: dict[str, dict] = {}
    if pids:
        rows = await con.fetch(
            """SELECT product_id, category_id, price FROM products
               WHERE product_id = ANY($1::text[])""",
            pids,
        )
        catalog = {r["product_id"]: {"category_id": r["category_id"], "price": float(r["price"])}
                   for r in rows}
    return normalize_order_items(items, catalog)


async def _top5_map(con: asyncpg.Connection, categories: list[str]) -> dict[str, list[str]]:
    cats = [c for c in set(categories) if c]
    if not cats:
        return {}
    # in_stock здесь, а не после подбора: выжженная категория должна отдать ход следующей,
    # а не отменять письмо, пока в заказе есть другая категория с живым топ-5.
    rows = await con.fetch(
        """SELECT t.category_id, t.product_id
           FROM top5_by_category t
           JOIN products p ON p.product_id = t.product_id AND p.in_stock
           WHERE t.category_id = ANY($1::text[])
           ORDER BY t.category_id, t.position""",
        cats,
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["category_id"], []).append(r["product_id"])
    return out


async def _load_products(con: asyncpg.Connection, product_ids: list[str]) -> list[dict]:
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
        """SELECT product_id, name, price, image_url, product_url FROM products
           WHERE product_id = ANY($1::text[]) AND in_stock""",
        ids,
    )
    by_id = {r["product_id"]: dict(r) for r in rows}
    # Сохраняем порядок из подбора (позиции топ-5).
    return [dict(by_id[pid], price=float(by_id[pid]["price"])) for pid in ids if pid in by_id]


def _demo() -> None:
    """Self-check чистой логики подбора (ТЗ 4.4)."""
    top5 = {"shoes": ["s1", "s2", "s3"], "bags": ["b1", "b2"]}
    # Один товар shoes s1 → shoes минус купленное s1 → [s2, s3].
    assert pick_cross_sell([{"product_id": "s1", "category_id": "shoes", "price": 5990}], top5) == ("shoes", ["s2", "s3"])
    # Несколько категорий → берём категорию самого дорогого (bags, 7000 > 5990).
    cat, ids = pick_cross_sell(
        [{"product_id": "s1", "category_id": "shoes", "price": 5990},
         {"product_id": "b1", "category_id": "bags", "price": 7000}], top5)
    assert cat == "bags" and ids == ["b2"], (cat, ids)
    # Основная категория выжжена (купили весь топ-5 bags) → фолбэк на shoes.
    cat, ids = pick_cross_sell(
        [{"product_id": "b1", "category_id": "bags", "price": 9000},
         {"product_id": "b2", "category_id": "bags", "price": 8000},
         {"product_id": "s1", "category_id": "shoes", "price": 5990}], top5)
    assert cat == "shoes" and ids == ["s2", "s3"], (cat, ids)
    # Совсем нечего предложить → (None, []).
    assert pick_cross_sell([{"product_id": "b1", "category_id": "bags", "price": 100},
                            {"product_id": "b2", "category_id": "bags", "price": 90}], top5) == (None, [])
    # Заказ с витрины без category_id: категорию берём из каталога, цену заказа не затираем.
    catalog = {"s1": {"category_id": "shoes", "price": 5990}}
    norm = normalize_order_items([{"product_id": " s1 ", "price": 100}], catalog)
    assert norm == [{"product_id": "s1", "category_id": "shoes", "price": 100.0}], norm
    # Цены в заказе нет — подставляем каталожную, иначе сортировка «самый дорогой» врёт.
    assert normalize_order_items([{"product_id": "s1"}], catalog)[0]["price"] == 5990.0
    # Явная категория заказа важнее каталога.
    assert normalize_order_items(
        [{"product_id": "s1", "category_id": "bags", "price": 1}], catalog)[0]["category_id"] == "bags"
    # Товара нет в каталоге и категории в заказе нет — позицию не из чего подбирать.
    assert normalize_order_items([{"product_id": "ghost", "price": 1}], catalog) == []
    print("postsale._demo OK")


if __name__ == "__main__":
    _demo()
