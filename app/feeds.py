"""Приём фидов от заказчика/магазина (Этап 0.1). Идемпотентные upsert'ы.

Форматы соответствуют контракту роадмапа. Если заказчик отдаёт CSV/другой транспорт —
меняется только парсер на входе, схема БД остаётся.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app import db, postsale

router = APIRouter(prefix="/feeds", tags=["feeds"])


class Category(BaseModel):
    category_id: str
    name: str
    sort_order: int


class Product(BaseModel):
    product_id: str
    name: str
    price: float
    image_url: Optional[str] = None
    category_id: str
    product_url: str
    in_stock: bool = True
    tags: list[str] = []  # членство в фидах: Новинка/Топ/Сопутствующий


class Top5Row(BaseModel):
    category_id: str
    position: int
    product_id: str


class OrderItem(BaseModel):
    product_id: str
    category_id: str
    price: float
    qty: int = 1


class Order(BaseModel):
    order_id: str
    user_id: str
    email: Optional[str] = None
    order_date: str  # ISO8601
    status: str
    items: list[OrderItem]


class Subscriber(BaseModel):
    user_id: str
    email: Optional[str] = None
    is_unsubscribed: bool = False
    consent_at: Optional[str] = None  # ISO8601; 152-ФЗ
    last_purchase_at: Optional[str] = None
    last_purchase_category_id: Optional[str] = None


# --- Ядро upsert'ов (принимают соединение) — переиспользуется endpoint'ами и XML-импортом. ---

async def upsert_subscribers_rows(con, rows: list[Subscriber]) -> None:
    await con.executemany(
        """INSERT INTO subscribers(user_id, email, is_unsubscribed, consent_at,
                                   last_purchase_at, last_purchase_category_id)
           VALUES($1, $2, $3, $4::text::timestamptz, $5::text::timestamptz, $6)
           ON CONFLICT (user_id) DO UPDATE SET
             email = EXCLUDED.email,
             is_unsubscribed = EXCLUDED.is_unsubscribed,
             consent_at = EXCLUDED.consent_at,
             last_purchase_at = EXCLUDED.last_purchase_at,
             last_purchase_category_id = EXCLUDED.last_purchase_category_id""",
        [(s.user_id, s.email, s.is_unsubscribed, s.consent_at,
          s.last_purchase_at, s.last_purchase_category_id) for s in rows],
    )


async def upsert_products_rows(con, rows: list[Product]) -> None:
    await con.executemany(
        """INSERT INTO products(product_id, name, price, image_url,
                                category_id, product_url, in_stock, tags, updated_at)
           VALUES($1, $2, $3, $4, $5, $6, $7, $8, now())
           ON CONFLICT (product_id) DO UPDATE SET
             name = EXCLUDED.name, price = EXCLUDED.price,
             image_url = EXCLUDED.image_url, category_id = EXCLUDED.category_id,
             product_url = EXCLUDED.product_url, in_stock = EXCLUDED.in_stock,
             -- union-merge тегов: товар накапливает членство в фидах (Новинка+Топ и т.п.)
             tags = COALESCE((SELECT array_agg(DISTINCT x)
                              FROM unnest(products.tags || EXCLUDED.tags) AS x), '{}'),
             updated_at = now()""",
        [(p.product_id, p.name, p.price, p.image_url,
          p.category_id, p.product_url, p.in_stock, p.tags) for p in rows],
    )


async def upsert_top5_rows(con, rows: list[Top5Row]) -> None:
    # Полная замена: присылается актуальный срез целиком.
    async with con.transaction():
        await con.execute("TRUNCATE top5_by_category")
        await con.executemany(
            """INSERT INTO top5_by_category(category_id, position, product_id, updated_at)
               VALUES($1, $2, $3, now())""",
            [(r.category_id, r.position, r.product_id) for r in rows],
        )


@router.put("/subscribers")
async def upsert_subscribers(rows: list[Subscriber]) -> dict:
    async with db.pool().acquire() as con:
        await upsert_subscribers_rows(con, rows)
    return {"upserted": len(rows)}


@router.put("/categories")
async def upsert_categories(rows: list[Category]) -> dict:
    async with db.pool().acquire() as con:
        await con.executemany(
            """INSERT INTO categories(category_id, name, sort_order)
               VALUES($1, $2, $3)
               ON CONFLICT (category_id) DO UPDATE
               SET name = EXCLUDED.name, sort_order = EXCLUDED.sort_order""",
            [(c.category_id, c.name, c.sort_order) for c in rows],
        )
    return {"upserted": len(rows)}


@router.put("/products")
async def upsert_products(rows: list[Product]) -> dict:
    async with db.pool().acquire() as con:
        await upsert_products_rows(con, rows)
    return {"upserted": len(rows)}


@router.put("/top5")
async def upsert_top5(rows: list[Top5Row]) -> dict:
    async with db.pool().acquire() as con:
        await upsert_top5_rows(con, rows)
    return {"replaced": len(rows)}


@router.put("/orders")
async def upsert_orders(rows: list[Order]) -> dict:
    async with db.pool().acquire() as con:
        await con.executemany(
            """INSERT INTO orders(order_id, user_id, email, order_date, status, items)
               VALUES($1, $2, $3, $4::text::timestamptz, $5, $6::jsonb)
               ON CONFLICT (order_id) DO UPDATE SET
                 status = EXCLUDED.status, items = EXCLUDED.items,
                 email = EXCLUDED.email""",
            [(o.order_id, o.user_id, o.email, o.order_date, o.status,
              json.dumps([i.model_dump() for i in o.items])) for o in rows],
        )
        # Триггер Постпродажи: ставим отложенную задачу на +N дней (идемпотентно).
        await postsale.enqueue_for_orders(con, rows)
    return {"upserted": len(rows)}
