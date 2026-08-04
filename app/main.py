"""Точка входа. Поднимает пул БД на старте, отдаёт /health с реальным пингом."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.config import settings
from app.admin import router as admin_router
from app.analytics import router as analytics_router
from app.cart import router as cart_router
from app.feeds import router as feeds_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.connect()
    yield
    await db.disconnect()


app = FastAPI(title="LeadHit-замена", lifespan=lifespan)

# CORS для триггер-сниппета: cart-ping шлётся кросс-доменно с groster.me.
# credentials не нужны — session_id едет в теле, а не в cookie сервиса.
_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

app.include_router(feeds_router)
app.include_router(cart_router)
app.include_router(analytics_router)
app.include_router(admin_router)


@app.get("/health")
async def health() -> dict:
    one = await db.pool().fetchval("SELECT 1")
    return {"status": "ok", "db": one == 1}
