"""Точка входа. Поднимает пул БД на старте, отдаёт /health с реальным пингом."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import auth, db
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


app = FastAPI(title="GrosterHit", lifespan=lifespan)

# CORS для триггер-сниппета: cart-ping шлётся кросс-доменно с groster.me.
# credentials не нужны — session_id едет в теле, а не в cookie сервиса.
_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# Гейт админки: /admin* доступен только с валидной сессией (см. app/auth.py).
# Пустой ADMIN_PASSWORD → вход отключён, пропускаем всё (дев / за NPM Basic Auth).
@app.middleware("http")
async def admin_gate(request, call_next):
    if request.url.path.startswith("/admin") and not auth.valid_session(request):
        return auth.unauthorized(request)
    return await call_next(request)


app.include_router(auth.router)
app.include_router(feeds_router)
app.include_router(cart_router)
app.include_router(analytics_router)
app.include_router(admin_router)


@app.get("/health")
async def health() -> dict:
    one = await db.pool().fetchval("SELECT 1")
    return {"status": "ok", "db": one == 1}
