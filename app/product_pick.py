"""Общий дедуп и сдвиг подборки: один клиент не должен получать одни и те же товары
в следующих письмах Best Offer / Постпродажи.
"""
from __future__ import annotations

# Сколько последних отправленных писем учитывать (ТЗ: 2–3; при лимите 30 товаров
# держим шире — иначе соседние письма почти совпадают).
DEDUP_LAST_N = 6


def rotate_ids(ids: list[str], offset: int) -> list[str]:
    """Циклический сдвиг списка: при offset=0 без изменений; иначе другой «старт» среза."""
    if not ids:
        return []
    n = len(ids)
    o = int(offset or 0) % n
    if o == 0:
        return list(ids)
    return ids[o:] + ids[:o]


async def recent_sent_products(con, user_id: str, last_n: int = DEDUP_LAST_N) -> set[str]:
    """product_id из последних N успешно отправленных писем Best Offer и Постпродажи."""
    n = max(1, int(last_n or DEDUP_LAST_N))
    rows = await con.fetch(
        """SELECT product_ids FROM email_log
           WHERE user_id = $1
             AND service = ANY($2::text[])
             AND status = 'sent'
           ORDER BY COALESCE(sent_at, created_at) DESC
           LIMIT $3""",
        user_id, ["best_offer", "postsale"], n,
    )
    recent: set[str] = set()
    for r in rows:
        for pid in (r["product_ids"] or []):
            if pid:
                recent.add(str(pid))
    return recent


async def send_offset(con, user_id: str, service: str) -> int:
    """Число успешных отправок сервиса — сдвигает срез внутри категории между письмами."""
    return int(await con.fetchval(
        """SELECT count(*) FROM email_log
           WHERE user_id = $1 AND service = $2 AND status = 'sent'""",
        user_id, service,
    ) or 0)


def _demo() -> None:
    assert rotate_ids(["a", "b", "c"], 0) == ["a", "b", "c"]
    assert rotate_ids(["a", "b", "c"], 1) == ["b", "c", "a"]
    assert rotate_ids(["a", "b", "c"], 3) == ["a", "b", "c"]
    assert rotate_ids([], 5) == []
    print("product_pick._demo OK")


if __name__ == "__main__":
    _demo()
