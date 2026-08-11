"""Точка входа. Поднимает пул БД на старте, отдаёт /health с реальным пингом."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import app_settings, auth, db
from app.admin import router as admin_router
from app.analytics import router as analytics_router
from app.cart import router as cart_router
from app.feeds import router as feeds_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.connect()
    # Адреса и домены из админки поверх .env. Не фатально: без них работаем на значениях
    # .env, а вот упавший старт означал бы 502 на всей витрине из-за одной настройки.
    try:
        async with db.pool().acquire() as con:
            await app_settings.load_site(con)
    except Exception as e:  # noqa: BLE001 — БД/таблица недоступна: логируем и живём на .env
        print(f"[startup] настройки адресов не прочитаны ({type(e).__name__}): работаем на .env")
    yield
    await db.disconnect()


app = FastAPI(title="GrosterHit", lifespan=lifespan)


class DynamicCORS(CORSMiddleware):
    """Домены витрины правятся в админке («Интеграция»), поэтому список читается на каждый
    запрос, а не фиксируется при старте: смена домена магазина не должна требовать рестарта.
    Пусто → пускаем любой origin (дев и «ещё не заполнили»), как и раньше с "*"."""

    def is_allowed_origin(self, origin: str) -> bool:
        raw = app_settings.site()["cors_origins"]
        allowed = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
        return not allowed or origin.rstrip("/") in allowed


# CORS для триггер-сниппета: cart-ping шлётся кросс-доменно с groster.me.
# credentials не нужны — session_id едет в теле, а не в cookie сервиса.
# allow_origins непустой и без "*" — иначе Starlette зашьёт заголовок "*" на старте и
# is_allowed_origin спрашивать не будет.
app.add_middleware(
    DynamicCORS,
    allow_origins=["https://dynamic.invalid"],
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
