"""JWT handler — emision y validacion de access tokens (HS256).

create_access_token(user_id, role) -> str   (payload {sub, role, iat, exp})
decode_access_token(token) -> TokenData | None   (None si expirado/firma invalida)

Puro: solo depende de settings (jwt_secret, jwt_alg, jwt_expire_min). PyJWT 2.x.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings


@dataclass(frozen=True)
class TokenData:
    user_id: int
    role: str


def create_access_token(user_id: int, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_min),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def decode_access_token(token: str) -> TokenData | None:
    """Decodifica y valida. None si el token es invalido, esta expirado, o le
    faltan campos obligatorios."""
    if not token:
        return None
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_alg]
        )
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    role = payload.get("role")
    if sub is None or role is None:
        return None
    try:
        return TokenData(user_id=int(sub), role=str(role))
    except (TypeError, ValueError):
        return None
