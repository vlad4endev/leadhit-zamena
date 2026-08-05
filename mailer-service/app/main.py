"""mailer-service: приём писем по API, очередь, отправка через провайдера, вебхуки ESP.

Авторассылка = основное приложение шлёт сюда письма триггеров; массовая рассылка = /v1/send/batch.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from app import sender, store, worker
from app.config import settings


class Message(BaseModel):
    to: str
    subject: str
    html: str
    from_email: str = ""
    from_name: str = ""
    meta: dict = {}


class Batch(BaseModel):
    messages: list[Message]


class EspEvent(BaseModel):
    message_id: str
    event: str   # delivered|opened|clicked|bounced|unsubscribed


class ProviderConfig(BaseModel):
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None   # пусто/не передан → пароль не меняется
    smtp_starttls: Optional[bool] = None
    mail_from: Optional[str] = None
    mail_from_name: Optional[str] = None
    rate_per_min: Optional[int] = None


class TestRequest(BaseModel):
    to: str


def _auth(authorization: Optional[str]) -> None:
    if settings.api_token and authorization != f"Bearer {settings.api_token}":
        raise HTTPException(401, "unauthorized")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await store.init()
    task = asyncio.create_task(worker.run())
    yield
    task.cancel()


app = FastAPI(title="mailer-service", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    cfg = await store.get_config()
    return {"status": "ok", "outbox": await store.stats(),
            "provider": "smtp" if cfg["smtp_host"] else "dev"}


@app.get("/v1/config")
async def get_config(authorization: Optional[str] = Header(default=None)) -> dict:
    """Настройки провайдера. Пароль не отдаём — только флаг has_password."""
    _auth(authorization)
    cfg = await store.get_config()
    has_password = bool(cfg.pop("smtp_password", ""))
    return {**cfg, "has_password": has_password, "provider": "smtp" if cfg["smtp_host"] else "dev"}


@app.put("/v1/config")
async def put_config(c: ProviderConfig, authorization: Optional[str] = Header(default=None)) -> dict:
    """Сохранить настройки провайдера. Пустой smtp_password не перезаписывает существующий."""
    _auth(authorization)
    patch = c.model_dump(exclude_none=True)
    if not patch.get("smtp_password"):
        patch.pop("smtp_password", None)
    if "smtp_starttls" in patch:
        patch["smtp_starttls"] = "true" if patch["smtp_starttls"] else "false"
    await store.set_config(patch)
    return {"ok": True}


@app.post("/v1/test")
async def test_send(r: TestRequest, authorization: Optional[str] = Header(default=None)) -> dict:
    """Тест-отправка с текущими настройками провайдера (синхронно, минуя очередь)."""
    _auth(authorization)
    try:
        mid = await sender.send(r.to, "Проверка mailer-service",
                                "<p>Тестовое письмо. Настройки почтового провайдера работают.</p>", "", "")
        return {"ok": True, "message_id": mid}
    except Exception as e:  # noqa: BLE001 — показываем ошибку провайдера оператору
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/v1/send")
async def send_one(m: Message, authorization: Optional[str] = Header(default=None)) -> dict:
    _auth(authorization)
    mid = await store.enqueue(m.to, m.subject, m.html, m.from_email, m.from_name, m.meta)
    return {"id": mid, "status": "queued"}


@app.post("/v1/send/sync")
async def send_one_sync(m: Message, authorization: Optional[str] = Header(default=None)) -> dict:
    """Синхронная отправка одного письма (тест-кнопки): минуя очередь, с реальным ответом ESP."""
    _auth(authorization)
    try:
        mid = await sender.send(m.to, m.subject, m.html, m.from_email, m.from_name)
        return {"ok": True, "message_id": mid}
    except Exception as e:  # noqa: BLE001 — показываем ошибку провайдера оператору
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/v1/send/batch")
async def send_batch(b: Batch, authorization: Optional[str] = Header(default=None)) -> dict:
    _auth(authorization)
    ids = [await store.enqueue(m.to, m.subject, m.html, m.from_email, m.from_name, m.meta)
           for m in b.messages]
    return {"queued": len(ids), "ids": ids}


@app.get("/v1/messages/{id}")
async def message(id: str) -> dict:
    row = await store.get(id)
    if not row:
        raise HTTPException(404, "not found")
    return {"id": row["id"], "state": row["state"], "attempts": row["attempts"],
            "message_id": row["message_id"], "last_event": row["last_event"]}


@app.post("/v1/esp/webhook")
async def esp_webhook(ev: EspEvent) -> dict:
    """Событие от провайдера. Мапим на письмо и пробрасываем в основное приложение с его meta."""
    import json
    row = await store.by_message_id(ev.message_id)
    if not row:
        return {"ok": False, "reason": "unknown message_id"}
    await store.set_event(row["id"], ev.event)
    await sender.callback(json.loads(row["meta"] or "{}"), ev.event)
    return {"ok": True}
