"""Dependencias de auth para FastAPI: get_current_user + require_role (RBAC).

Logica extraida y testeable (sin Request):
  - _resolve_user(token, users) -> User   (raise 401 si token invalido / usuario
    inexistente / inactivo)
  - check_role(user, allowed) -> User      (raise 403 si rol no permitido)

Los wrappers get_current_user / require_role son finos (extraen creds del header
y delegan). Frontera DB: UserRepository (inyectable / Tier B).
"""
from __future__ import annotations

from collections.abc import Iterable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt_handler import decode_access_token
from app.config import settings
from app.db.base import get_session
from app.db.models import User, UserRole
from app.db.repositories import UserRepository

_bearer = HTTPBearer(auto_error=False)


async def _resolve_user(token: str, users: UserRepository) -> User:
    """Core: token valido + usuario activo -> User. Raise 401 si no."""
    data = decode_access_token(token)
    if data is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token invalido o expirado")
    user = await users.get(data.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Usuario no encontrado o inactivo"
        )
    if data.role != user.role.value:
        # el rol cambio desde la emision del token -> forzar re-login
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token obsoleto (rol cambiado)")
    return user


def check_role(user: User, allowed: Iterable[UserRole]) -> User:
    """Pure: raise 403 si user.role no esta en allowed."""
    if user.role not in allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Rol '{user.role.value}' no permitido para esta operacion",
        )
    return user


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Autenticacion requerida",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await _resolve_user(creds.credentials, UserRepository(session))


def require_role(*allowed: UserRole):
    """Dependency factory RBAC: depende de get_current_user y aplica check_role."""

    async def _dep(user: User = Depends(get_current_user)) -> User:
        return check_role(user, allowed)

    return _dep


# Re-exporta para que la app pueda marcar endpoints como admin-only de forma legible.
admin_only = require_role(UserRole.ADMIN)
receptionist_or_admin = require_role(UserRole.ADMIN, UserRole.RECEPCIONISTA)
