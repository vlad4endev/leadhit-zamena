"""Интеграционный прогон трёх сценариев авторассылки: реально шлём через RecordingMailer.

Проверяет: consent (152-ФЗ), пустые товары, отправка Best Offer / cart / postsale,
откат таймеров при event=failed. Требует локальную БД (make db).

Запуск: ./.venv/bin/python scripts/test_scenarios_send.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analytics, best_offer, cart, db, postsale  # noqa: E402


class RecordingMailer:
    def __init__(self, fail_ids: set[str] | None = None):
        self.sent: list[dict] = []
        self.fail_ids = fail_ids or set()

    async def send(self, to, subject, html, from_email="", from_name="", meta=None):
        if to in self.fail_ids:
            return False
        self.sent.append({"to": to, "subject": subject, "html": html,
                          "from_email": from_email, "meta": meta or {}})
        return True


async def _seed(con) -> None:
    """Минимальный каталог + подписчики для всех трёх сценариев."""
    await con.execute("TRUNCATE email_log, send_queue, cart_sessions, orders, "
                      "top5_by_category, products, subscribers, categories CASCADE")
    await con.executemany(
        "INSERT INTO categories(category_id, name, sort_order) VALUES($1,$2,$3)",
        [("shoes", "Обувь", 1), ("bags", "Сумки", 2), ("acc", "Аксессуары", 3)],
    )
    await con.executemany(
        """INSERT INTO products(product_id, name, price, category_id, product_url, in_stock)
           VALUES($1,$2,$3,$4,$5,$6)""",
        [
            ("s1", "Alpha", 5990, "shoes", "https://g/s1", True),
            ("s2", "Beta", 7490, "shoes", "https://g/s2", True),
            ("s3", "Gamma", 4290, "shoes", "https://g/s3", True),
            ("b1", "Delta", 3990, "bags", "https://g/b1", True),
            ("b2", "Epsilon", 6490, "bags", "https://g/b2", True),
            ("a1", "Zeta", 1990, "acc", "https://g/a1", True),
            ("out1", "OOS", 100, "shoes", "https://g/out", False),
        ],
    )
    await con.executemany(
        "INSERT INTO top5_by_category(category_id, position, product_id) VALUES($1,$2,$3)",
        [("shoes", 1, "s1"), ("shoes", 2, "s2"), ("shoes", 3, "s3"),
         ("bags", 1, "b1"), ("bags", 2, "b2"), ("acc", 1, "a1")],
    )
    # u_ok — с согласием; u_nocon — без consent_at; u_unsub — отписан.
    await con.executemany(
        """INSERT INTO subscribers(user_id, email, is_unsubscribed, consent_at,
                                   last_sent_best_offer_at)
           VALUES($1,$2,$3,$4,$5)""",
        [
            ("u_ok", "ok@example.com", False, datetime(2025, 1, 1, tzinfo=timezone.utc), None),
            ("u_nocon", "nocon@example.com", False, None, None),
            ("u_unsub", "unsub@example.com", True,
             datetime(2025, 1, 1, tzinfo=timezone.utc), None),
            ("u_cart", "cart@example.com", False,
             datetime(2025, 1, 1, tzinfo=timezone.utc), None),
            ("u_post", "post@example.com", False,
             datetime(2025, 1, 1, tzinfo=timezone.utc), None),
        ],
    )


async def test_best_offer(con) -> None:
    mailer = RecordingMailer()
    n = await best_offer.run_batch(con, mailer=mailer, force=True)
    assert n >= 1, f"best_offer expected sends, got {n}"
    tos = {m["to"] for m in mailer.sent}
    assert "ok@example.com" in tos, tos
    assert "nocon@example.com" not in tos, "без consent_at не шлём"
    assert "unsub@example.com" not in tos, "отписанным не шлём"
    # HTML не пустой по товарам.
    assert any("Alpha" in m["html"] or "Beta" in m["html"] for m in mailer.sent)
    log = await con.fetchrow(
        "SELECT status, product_ids FROM email_log WHERE user_id='u_ok' AND service='best_offer'")
    assert log and log["status"] == "sent" and len(log["product_ids"]) >= 1
    print("  best_offer OK", f"sent={n}", f"to={sorted(tos)}")


async def test_best_offer_oos_skip(con) -> None:
    """Если весь top5 категории out-of-stock — письмо не уходит."""
    await con.execute("UPDATE products SET in_stock = FALSE")
    await con.execute(
        """UPDATE subscribers SET last_sent_best_offer_at = NULL, last_any_trigger_at = NULL,
               rotation_pointer_category_id = NULL WHERE user_id = 'u_ok'""")
    await con.execute("DELETE FROM email_log WHERE user_id = 'u_ok'")
    mailer = RecordingMailer()
    n = await best_offer.run_batch(con, mailer=mailer, force=True)
    assert n == 0 and mailer.sent == [], (n, mailer.sent)
    await con.execute("UPDATE products SET in_stock = TRUE WHERE product_id <> 'out1'")
    print("  best_offer OOS-skip OK")


async def test_cart(con) -> None:
    mailer = RecordingMailer()
    # Сессия уже departed + grace прошёл.
    await con.execute(
        """INSERT INTO cart_sessions(session_id, user_id, email, cart_items, state,
                                     last_ping_at, departed_at, created_at)
           VALUES('s1', 'u_cart', 'cart@example.com',
                  $1::jsonb, 'departed',
                  now() - interval '1 hour', now() - interval '30 minutes',
                  now() - interval '2 hours')""",
        json.dumps([{"product_id": "s1", "qty": 1}, {"product_id": "b1", "qty": 2}]),
    )
    # Сессия без согласия — не должна уйти.
    await con.execute(
        """INSERT INTO cart_sessions(session_id, user_id, email, cart_items, state,
                                     last_ping_at, departed_at, created_at)
           VALUES('s_nocon', 'u_nocon', 'nocon@example.com',
                  $1::jsonb, 'departed',
                  now() - interval '1 hour', now() - interval '30 minutes',
                  now() - interval '2 hours')""",
        json.dumps([{"product_id": "s1", "qty": 1}]),
    )
    n = await cart.run_due(con, mailer=mailer, force=True)
    assert n == 1, f"cart expected 1 send, got {n}: {mailer.sent}"
    assert mailer.sent[0]["to"] == "cart@example.com"
    assert "Alpha" in mailer.sent[0]["html"] or "s1" in mailer.sent[0]["html"]
    st = await con.fetchval("SELECT state FROM cart_sessions WHERE session_id='s1'")
    assert st == "sent"
    st2 = await con.fetchval("SELECT state FROM cart_sessions WHERE session_id='s_nocon'")
    assert st2 == "sent"  # закрыта без отправки (no_consent)
    print("  cart OK")


async def test_postsale(con) -> None:
    mailer = RecordingMailer()
    # Сброс антидубля: best_offer в этом прогоне уже мог отметить last_any_trigger_at.
    await con.execute(
        "UPDATE subscribers SET last_any_trigger_at = NULL, last_sent_postsale_at = NULL "
        "WHERE user_id = 'u_post'")
    # Заказ 8 дней назад → run_after уже наступил.
    order_date = datetime.now(timezone.utc) - timedelta(days=8)
    await con.execute(
        """INSERT INTO orders(order_id, user_id, email, order_date, status, items)
           VALUES('o_post', 'u_post', 'post@example.com', $1, 'paid', $2::jsonb)""",
        order_date,
        json.dumps([{"product_id": "s1", "category_id": "shoes", "price": 5990, "qty": 1}]),
    )
    # enqueue_for_orders не ставит задачи с run_after в прошлом — кладём вручную.
    await con.execute(
        """INSERT INTO send_queue(user_id, service, order_id, run_after, state)
           VALUES('u_post', 'postsale', 'o_post', now() - interval '1 hour', 'scheduled')""")

    n = await postsale.run_due(con, mailer=mailer, force=True)
    assert n == 1, f"postsale expected 1, got {n}: {mailer.sent}"
    assert mailer.sent[0]["to"] == "post@example.com"
    # Cross-sell shoes минус купленное s1 → s2/s3.
    assert "Beta" in mailer.sent[0]["html"] or "Gamma" in mailer.sent[0]["html"]
    state = await con.fetchval(
        "SELECT state FROM send_queue WHERE order_id='o_post' AND service='postsale'")
    assert state == "sent"
    print("  postsale OK")


async def _subscriber(con, user_id: str, email: str) -> None:
    await con.execute(
        """INSERT INTO subscribers(user_id, email, is_unsubscribed, consent_at)
           VALUES($1, $2, FALSE, now())
           ON CONFLICT (user_id) DO UPDATE SET
             email = EXCLUDED.email, is_unsubscribed = FALSE, consent_at = now(),
             last_sent_best_offer_at = NULL, last_sent_cart_at = NULL,
             last_sent_postsale_at = NULL, last_any_trigger_at = NULL,
             rotation_pointer_category_id = NULL""",
        user_id, email,
    )


async def test_best_offer_skips_thin_stock(con) -> None:
    """Категория с 1 товаром в наличии (<2) не должна уехать письмом — берём следующую."""
    await con.execute("UPDATE products SET in_stock = FALSE WHERE product_id IN ('s2', 's3')")
    try:
        await _subscriber(con, "u_part", "part@example.com")
        mailer = RecordingMailer()
        await best_offer.run_batch(con, mailer=mailer, force=True)
        html = next(m["html"] for m in mailer.sent if m["to"] == "part@example.com")
        assert "Delta" in html and "Epsilon" in html, html
        assert "Alpha" not in html, "shoes остался 1 в наличии — категорию надо пропустить"
        ids = await con.fetchval(
            "SELECT product_ids FROM email_log WHERE user_id='u_part' AND service='best_offer'")
        assert set(ids) == {"b1", "b2"}, ids
    finally:
        await con.execute(
            "UPDATE products SET in_stock = TRUE WHERE product_id IN ('s2', 's3')")
    print("  best_offer thin-stock skip OK")


async def test_cart_skips_oos(con) -> None:
    await _subscriber(con, "u_cart2", "cart2@example.com")
    await con.execute(
        """INSERT INTO cart_sessions(session_id, user_id, email, cart_items, state,
                                     last_ping_at, departed_at, created_at)
           VALUES('s_oos', 'u_cart2', 'cart2@example.com',
                  $1::jsonb, 'departed',
                  now() - interval '1 hour', now() - interval '30 minutes',
                  now() - interval '2 hours')""",
        json.dumps([
            {"product_id": "s1", "qty": 1},
            {"product_id": "out1", "qty": 1},
            {"product_id": " s1 ", "qty": 3},
        ]),
    )
    mailer = RecordingMailer()
    n = await cart.run_due(con, mailer=mailer, force=True)
    assert n == 1, mailer.sent
    html = mailer.sent[0]["html"]
    assert "Alpha" in html and "OOS" not in html, html
    ids = await con.fetchval(
        "SELECT product_ids FROM email_log WHERE user_id='u_cart2' AND service='cart'")
    assert list(ids) == ["s1"], ids
    print("  cart OOS-skip OK")


async def test_postsale_without_category(con) -> None:
    """Заказ с витрины без category_id всё равно подбирает cross-sell по каталогу."""
    await _subscriber(con, "u_post2", "post2@example.com")
    await con.execute(
        """INSERT INTO orders(order_id, user_id, email, order_date, status, items)
           VALUES('o_nocat', 'u_post2', 'post2@example.com', now() - interval '8 days', 'paid',
                  $1::jsonb)""",
        json.dumps([{"product_id": "s1", "price": 5990, "qty": 1}]),
    )
    await con.execute(
        """INSERT INTO send_queue(user_id, service, order_id, run_after, state)
           VALUES('u_post2', 'postsale', 'o_nocat', now() - interval '1 hour', 'scheduled')""")
    mailer = RecordingMailer()
    n = await postsale.run_due(con, mailer=mailer, force=True)
    assert n == 1, mailer.sent
    html = mailer.sent[0]["html"]
    assert "Beta" in html and "Gamma" in html, html
    assert "Alpha" not in html
    print("  postsale no-category OK")


async def test_postsale_oos_fallback(con) -> None:
    """Топ-5 дорогой категории выжжен остатком → cross-sell следующей категории заказа."""
    await con.execute("UPDATE products SET in_stock = FALSE WHERE product_id IN ('s2', 's3')")
    try:
        await _subscriber(con, "u_post3", "post3@example.com")
        await con.execute(
            """INSERT INTO orders(order_id, user_id, email, order_date, status, items)
               VALUES('o_oos', 'u_post3', 'post3@example.com', now() - interval '8 days', 'paid',
                      $1::jsonb)""",
            json.dumps([
                {"product_id": "s1", "price": 9000, "qty": 1},
                {"product_id": "b1", "price": 1000, "qty": 1},
            ]),
        )
        await con.execute(
            """INSERT INTO send_queue(user_id, service, order_id, run_after, state)
               VALUES('u_post3', 'postsale', 'o_oos', now() - interval '1 hour', 'scheduled')""")
        mailer = RecordingMailer()
        n = await postsale.run_due(con, mailer=mailer, force=True)
        assert n == 1, mailer.sent
        html = mailer.sent[0]["html"]
        assert "Epsilon" in html, html
        assert "Alpha" not in html and "Beta" not in html
    finally:
        await con.execute(
            "UPDATE products SET in_stock = TRUE WHERE product_id IN ('s2', 's3')")
    print("  postsale OOS-fallback OK")


async def test_failed_rollback(con) -> None:
    """HttpMailer принял в очередь → сценарий сдвинул таймеры → ESP failed → откат."""
    # Готовим «уже отправленное» Best Offer письмо.
    await con.execute(
        """UPDATE subscribers SET last_sent_best_offer_at = now(), last_any_trigger_at = now()
           WHERE user_id = 'u_ok'""")
    log_id = await con.fetchval(
        """INSERT INTO email_log(user_id, service, product_ids, status, sent_at)
           VALUES('u_ok', 'best_offer', ARRAY['s1'], 'sent', now()) RETURNING id""")
    sent_at = await con.fetchval("SELECT sent_at FROM email_log WHERE id=$1", log_id)
    await con.execute(
        "UPDATE subscribers SET last_sent_best_offer_at=$1, last_any_trigger_at=$1 WHERE user_id='u_ok'",
        sent_at)

    await analytics.rollback_failed_send(con, log_id)
    row = await con.fetchrow(
        "SELECT last_sent_best_offer_at, last_any_trigger_at FROM subscribers WHERE user_id='u_ok'")
    assert row["last_sent_best_offer_at"] is None, row
    assert row["last_any_trigger_at"] is None, row

    # Postsale: failed → задача снова scheduled, лог удалён (уник-индекс).
    await con.execute(
        """INSERT INTO orders(order_id, user_id, email, order_date, status, items)
           VALUES('o_fail', 'u_post', 'post@example.com', now() - interval '10 days', 'paid',
                  '[{"product_id":"s1","category_id":"shoes","price":1,"qty":1}]'::jsonb)
           ON CONFLICT DO NOTHING""")
    await con.execute(
        """INSERT INTO send_queue(user_id, service, order_id, run_after, state)
           VALUES('u_post', 'postsale', 'o_fail', now(), 'sent')
           ON CONFLICT DO NOTHING""")
    # Если конфликт по order_id — форсим state.
    await con.execute(
        "UPDATE send_queue SET state='sent' WHERE order_id='o_fail' AND service='postsale'")
    lid = await con.fetchval(
        """INSERT INTO email_log(user_id, service, product_ids, order_id, status, sent_at)
           VALUES('u_post', 'postsale', ARRAY['s2'], 'o_fail', 'sent', now())
           ON CONFLICT DO NOTHING RETURNING id""")
    if lid is None:
        lid = await con.fetchval(
            "SELECT id FROM email_log WHERE order_id='o_fail' AND service='postsale'")
        await con.execute(
            "UPDATE email_log SET status='sent', sent_at=now() WHERE id=$1", lid)
    await analytics.rollback_failed_send(con, lid)
    st = await con.fetchval(
        "SELECT state FROM send_queue WHERE order_id='o_fail' AND service='postsale'")
    assert st == "scheduled", st
    assert await con.fetchval(
        "SELECT count(*) FROM email_log WHERE id=$1", lid) == 0
    print("  failed-rollback OK")


async def main() -> None:
    await db.connect()
    try:
        async with db.pool().acquire() as con:
            print("scenario send integration:")
            await _seed(con)
            await test_best_offer(con)
            await test_best_offer_oos_skip(con)
            await test_cart(con)
            await test_postsale(con)
            await test_best_offer_skips_thin_stock(con)
            await test_cart_skips_oos(con)
            await test_postsale_without_category(con)
            await test_postsale_oos_fallback(con)
            await test_failed_rollback(con)
        print("scenario send integration OK")
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
