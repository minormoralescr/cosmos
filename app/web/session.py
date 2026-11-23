"""Sesion web por cookie firmada (itsdangerous). Mas natural que Bearer JWT para
una UI server-rendered: el login form setea la cookie, las rutas web la leen.

  - create/read_session_cookie(user_id)
  - set/clear_session_cookie(response)
  - current_user (dep): lee cookie -> carga User; si no -> 302 a /login
  - require_web_admin (dep): 403 si no es ADMIN
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.base import get_session
from app.db.models import User, UserRole
from app.db.repositories import UserRepository

_serializer = URLSafeTimedSerializer(settings.jwt_secret, salt="spa-web-session")
COOKIE_NAME = "spa_session"
_MAX_AGE = settings.jwt_expire_min * 60


def create_session_cookie(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def read_session_cookie(token: str | None) -> int | None:
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    try:
        return int(uid) if uid is not None else None
    except (TypeError, ValueError):
        return None


def set_session_cookie(response: Response, user_id: int) -> None:
    response.set_cookie(
        COOKIE_NAME, create_session_cookie(user_id),
        max_age=_MAX_AGE, httponly=True, samesite="lax", secure=settings.is_prod,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


def _redirect_login() -> HTTPException:
    return HTTPException(status_code=302, headers={"Location": "/login"})


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User:
    uid = read_session_cookie(request.cookies.get(COOKIE_NAME))
    if uid is None:
        raise _redirect_login()
    user = await UserRepository(session).get(uid)
    if user is None or not user.is_active:
        raise _redirect_login()
    return user


async def require_web_admin(user: User = Depends(current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Acceso solo administradores")
    return user
