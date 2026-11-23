"""Router de autenticacion: POST /auth/login + GET /auth/me.

  - authenticate(email, password, users) -> User   (raise 401 si bad creds)
  - login route: valida credenciales, emite JWT, devuelve {access_token, user}
  - me route: devuelve el usuario autenticado (dep get_current_user)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.jwt_handler import create_access_token
from app.auth.security import verify_password
from app.db.base import get_session
from app.db.models import User
from app.db.repositories import UserRepository

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    role: str
    agent_id: int | None


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role.value,
        agent_id=user.agent_id,
    )


async def authenticate(
    email: str, password: str, users: UserRepository
) -> User:
    """Core: email+password validos + usuario activo -> User. Raise 401 si no.
    Mensaje generico para no revelar si el email existe (anti enumeracion)."""
    user = await users.get_by_email(email)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciales invalidas")
    if not verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciales invalidas")
    return user


@router.post("/login", response_model=LoginOut)
async def login(body: LoginIn, session: AsyncSession = Depends(get_session)) -> LoginOut:
    user = await authenticate(body.email, body.password, UserRepository(session))
    token = create_access_token(user.id, user.role.value)
    return LoginOut(access_token=token, user=_user_out(user))


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return _user_out(user)
