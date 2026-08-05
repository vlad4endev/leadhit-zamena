"""Авторизация админки: один пароль из окружения + подписанная кука-сессия (stdlib hmac).

Пустой ADMIN_PASSWORD → вход отключён (дев / защита на NPM Basic Auth, см. nginx.npm.conf).
Кука: base64("username:exp") + "." + hmac_sha256(secret). Подпись = SESSION_SECRET или пароль.
Гейт (см. main.py) закрывает /admin*: HTML-запрос без сессии → редирект на /login, XHR → 401.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from app.config import settings

router = APIRouter(tags=["auth"])

COOKIE = "gh_session"
_TTL = 7 * 24 * 3600  # сессия живёт неделю
_PAGE = os.path.join(os.path.dirname(__file__), "static", "login.html")


def enabled() -> bool:
    return bool(settings.admin_password)


def _secret() -> bytes:
    return (settings.session_secret or settings.admin_password or "dev").encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def _make_token(username: str) -> str:
    payload = f"{username}:{int(time.time()) + _TTL}"
    b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"{b64}.{_sign(payload)}"


def valid_session(request: Request) -> bool:
    if not enabled():
        return True
    token = request.cookies.get(COOKIE)
    if not token or "." not in token:
        return False
    b64, sig = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(b64.encode()).decode()
    except Exception:  # noqa: BLE001
        return False
    if not hmac.compare_digest(sig, _sign(payload)):  # подпись подделана
        return False
    _, _, exp = payload.rpartition(":")
    return exp.isdigit() and int(exp) > time.time()  # не истекла


def unauthorized(request: Request) -> Response:
    """HTML-навигация → на страницу входа; fetch/XHR → 401 (SPA сам решит редиректить)."""
    if "text/html" in request.headers.get("accept", ""):
        return Response(status_code=303, headers={"Location": "/login"})
    return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)


def _set_cookie(resp: Response, request: Request, username: str) -> None:
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(COOKIE, _make_token(username), max_age=_TTL,
                    httponly=True, samesite="lax", secure=secure, path="/")


@router.get("/login")
async def login_page(request: Request) -> Response:
    if not enabled() or valid_session(request):
        return Response(status_code=303, headers={"Location": "/admin"})
    return FileResponse(_PAGE, headers={"Cache-Control": "no-cache"})


@router.post("/login")
async def login(request: Request) -> Response:
    if not enabled():
        return JSONResponse({"ok": True})  # вход отключён — считаем всех авторизованными
    body = await request.json()
    user = str(body.get("username") or "")
    pwd = str(body.get("password") or "")
    ok = (hmac.compare_digest(user, settings.admin_username)
          and hmac.compare_digest(pwd, settings.admin_password))
    if not ok:
        return JSONResponse({"ok": False, "reason": "Неверный логин или пароль"}, status_code=401)
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, request, user)
    return resp


@router.post("/logout")
async def logout() -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


def _demo() -> None:
    # Валидная кука проходит; подделка подписи и истёкший срок — нет.
    settings.admin_password = "secret"
    settings.session_secret = ""

    class Req:  # минимальный дубль Request для проверки логики
        def __init__(self, token):
            self.cookies = {COOKIE: token} if token else {}
    good = _make_token("admin")
    assert valid_session(Req(good))
    assert not valid_session(Req(None))
    assert not valid_session(Req(good[:-1] + ("0" if good[-1] != "0" else "1")))  # битая подпись
    b64, sig = good.rsplit(".", 1)
    payload = base64.urlsafe_b64decode(b64).decode()
    expired = f"{payload.rsplit(':', 1)[0]}:{int(time.time()) - 1}"
    exp_tok = base64.urlsafe_b64encode(expired.encode()).decode() + "." + _sign(expired)
    assert not valid_session(Req(exp_tok))
    settings.admin_password = ""  # выключенный вход пускает всех
    assert valid_session(Req(None))
    print("auth._demo OK")


if __name__ == "__main__":
    _demo()
