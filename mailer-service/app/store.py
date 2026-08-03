"""Outbox на SQLite. Микросервис самодостаточен — своя очередь, без внешней БД.

Одно соединение под потокобезопасным доступом; все вызовы идут через asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid

from app.config import settings

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id          TEXT PRIMARY KEY,
    to_addr     TEXT NOT NULL,
    subject     TEXT NOT NULL,
    html        TEXT NOT NULL,
    from_email  TEXT,
    from_name   TEXT,
    meta        TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL DEFAULT 'queued',   -- queued|sent|failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    message_id  TEXT,
    last_event  TEXT,
    created_at  REAL NOT NULL,
    sent_at     REAL
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state, next_attempt);
CREATE INDEX IF NOT EXISTS outbox_msgid ON outbox(message_id);
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
"""

# Ключи конфигурации провайдера (переопределяют .env).
_CFG_KEYS = ("smtp_host", "smtp_port", "smtp_user", "smtp_password",
             "smtp_starttls", "mail_from", "mail_from_name", "rate_per_min")


def config_sync() -> dict:
    """Актуальные настройки провайдера: значения из БД поверх дефолтов .env (синхронно)."""
    with _lock:
        rows = _conn.execute("SELECT key, value FROM config").fetchall()
    db = {r["key"]: r["value"] for r in rows}

    def s(k, default):
        v = db.get(k)
        return v if v not in (None, "") else default

    starttls = db.get("smtp_starttls")
    return {
        "smtp_host": s("smtp_host", settings.smtp_host),
        "smtp_port": int(s("smtp_port", settings.smtp_port)),
        "smtp_user": s("smtp_user", settings.smtp_user),
        "smtp_password": s("smtp_password", settings.smtp_password),
        "smtp_starttls": (starttls.lower() in ("1", "true", "yes")) if starttls else settings.smtp_starttls,
        "mail_from": s("mail_from", settings.mail_from),
        "mail_from_name": s("mail_from_name", settings.mail_from_name),
        "rate_per_min": int(s("rate_per_min", settings.rate_per_min)),
    }


def _init_sync() -> None:
    global _conn
    _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(_SCHEMA)
    _conn.commit()


def _exec(sql: str, params=()):
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def _query(sql: str, params=()):
    with _lock:
        return _conn.execute(sql, params).fetchall()


async def init() -> None:
    await asyncio.to_thread(_init_sync)


async def enqueue(to: str, subject: str, html: str, from_email: str, from_name: str, meta: dict) -> str:
    mid = uuid.uuid4().hex
    await asyncio.to_thread(
        _exec,
        """INSERT INTO outbox(id, to_addr, subject, html, from_email, from_name, meta,
                              next_attempt, created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (mid, to, subject, html, from_email, from_name, json.dumps(meta), time.time(), time.time()),
    )
    return mid


async def due(limit: int) -> list[dict]:
    rows = await asyncio.to_thread(
        _query,
        "SELECT * FROM outbox WHERE state='queued' AND next_attempt<=? ORDER BY created_at LIMIT ?",
        (time.time(), limit),
    )
    return [dict(r) for r in rows]


async def mark_sent(id: str, message_id: str) -> None:
    await asyncio.to_thread(
        _exec, "UPDATE outbox SET state='sent', message_id=?, sent_at=? WHERE id=?",
        (message_id, time.time(), id),
    )


async def mark_retry(id: str, attempts: int, next_attempt: float) -> None:
    await asyncio.to_thread(
        _exec, "UPDATE outbox SET attempts=?, next_attempt=? WHERE id=?", (attempts, next_attempt, id))


async def mark_failed(id: str, attempts: int) -> None:
    await asyncio.to_thread(
        _exec, "UPDATE outbox SET state='failed', attempts=? WHERE id=?", (attempts, id))


async def get(id: str) -> dict | None:
    rows = await asyncio.to_thread(_query, "SELECT * FROM outbox WHERE id=?", (id,))
    return dict(rows[0]) if rows else None


async def by_message_id(message_id: str) -> dict | None:
    rows = await asyncio.to_thread(_query, "SELECT * FROM outbox WHERE message_id=?", (message_id,))
    return dict(rows[0]) if rows else None


async def set_event(id: str, event: str) -> None:
    await asyncio.to_thread(_exec, "UPDATE outbox SET last_event=? WHERE id=?", (event, id))


async def stats() -> dict:
    rows = await asyncio.to_thread(_query, "SELECT state, count(*) c FROM outbox GROUP BY state")
    return {r["state"]: r["c"] for r in rows}


async def get_config() -> dict:
    return await asyncio.to_thread(config_sync)


def _set_config_sync(patch: dict) -> None:
    with _lock:
        for k, v in patch.items():
            if k in _CFG_KEYS and v is not None:
                _conn.execute(
                    "INSERT INTO config(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
        _conn.commit()


async def set_config(patch: dict) -> None:
    await asyncio.to_thread(_set_config_sync, patch)
