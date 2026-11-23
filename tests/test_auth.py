"""Tests de auth: security (bcrypt), jwt_handler (PyJWT), dependencies (_resolve_user
+ check_role) y authenticate. Frontera DB (UserRepository) fake. JWT/bcrypt reales.

Estrategia (Directiva #4):
  - hash/verify, encode/decode, check_role: puros/reales (sin mock).
  - _resolve_user / authenticate: fake UserRepository (frontera DB).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from app.auth.jwt_handler import TokenData, create_access_token, decode_access_token
from app.auth.security import hash_password, verify_password
from app.config import settings
from app.db.models import User, UserRole
from app.auth.dependencies import _resolve_user, check_role
from app.api.auth import authenticate


# --- security (bcrypt real) ---

def test_hash_and_verify_roundtrip():
    h = hash_password("secreto123")
    assert h != "secreto123"
    assert verify_password("secreto123", h) is True


def test_verify_wrong_password_false():
    assert verify_password("otra", hash_password("secreto123")) is False


def test_hash_has_random_salt_differs_per_call():
    assert hash_password("misma") != hash_password("misma")


def test_verify_empty_or_invalid_returns_false():
    assert verify_password("", "x") is False
    assert verify_password("x", "") is False
    assert verify_password("x", "no-es-bcrypt") is False


def test_hash_empty_raises():
    with pytest.raises(ValueError):
        hash_password("")


# --- jwt_handler (PyJWT real) ---

def test_jwt_roundtrip():
    token = create_access_token(7, "admin")
    data = decode_access_token(token)
    assert data == TokenData(user_id=7, role="admin")


def test_jwt_expired_returns_none():
    now = datetime.now(timezone.utc)
    expired = pyjwt.encode(
        {"sub": "1", "role": "admin", "exp": now - timedelta(seconds=10)},
        settings.jwt_secret,
        algorithm=settings.jwt_alg,
    )
    assert decode_access_token(expired) is None


def test_jwt_invalid_signature_returns_none():
    token = create_access_token(1, "admin")
    # firma con otra secret
    bad = pyjwt.encode(
        {"sub": "1", "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        "otra-secret",
        algorithm=settings.jwt_alg,
    )
    assert decode_access_token(bad) is None


def test_jwt_malformed_returns_none():
    assert decode_access_token("no.es.un.jwt") is None
    assert decode_access_token("") is None


def test_jwt_missing_fields_returns_none():
    token = pyjwt.encode(
        {"foo": "bar", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        settings.jwt_secret,
        algorithm=settings.jwt_alg,
    )
    assert decode_access_token(token) is None


# --- Fake UserRepository ---

class _FakeUserRepo:
    def __init__(self, users: dict[int, User]):
        self._by_id = users

    async def get(self, uid):
        return self._by_id.get(uid)

    async def get_by_email(self, email):
        return next((u for u in self._by_id.values() if u.email == email), None)


def _user(uid=1, email="a@b.com", role=UserRole.ADMIN, active=True, pw="secret123"):
    return User(
        id=uid, email=email, password_hash=hash_password(pw), full_name="Ana",
        role=role, is_active=active, agent_id=None,
    )


# --- _resolve_user ---

@pytest.mark.asyncio
async def test_resolve_user_valid_token():
    user = _user()
    token = create_access_token(user.id, user.role.value)
    got = await _resolve_user(token, _FakeUserRepo({user.id: user}))
    assert got.id == user.id


@pytest.mark.asyncio
async def test_resolve_user_invalid_token_raises_401():
    with pytest.raises(HTTPException) as exc:
        await _resolve_user("bad.token", _FakeUserRepo({}))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_resolve_user_unknown_user_raises_401():
    token = create_access_token(999, "admin")
    with pytest.raises(HTTPException) as exc:
        await _resolve_user(token, _FakeUserRepo({}))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_resolve_user_inactive_raises_401():
    user = _user(active=False)
    token = create_access_token(user.id, user.role.value)
    with pytest.raises(HTTPException) as exc:
        await _resolve_user(token, _FakeUserRepo({user.id: user}))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_resolve_user_role_changed_raises_401():
    user = _user(role=UserRole.RECEPCIONISTA)
    token = create_access_token(user.id, UserRole.ADMIN.value)  # rol obsoleto
    with pytest.raises(HTTPException) as exc:
        await _resolve_user(token, _FakeUserRepo({user.id: user}))
    assert exc.value.status_code == 401


# --- check_role (pure) ---

def test_check_role_allowed_returns_user():
    user = _user(role=UserRole.ADMIN)
    assert check_role(user, [UserRole.ADMIN, UserRole.RECEPCIONISTA]) is user


def test_check_role_disallowed_raises_403():
    user = _user(role=UserRole.RECEPCIONISTA)
    with pytest.raises(HTTPException) as exc:
        check_role(user, [UserRole.ADMIN])
    assert exc.value.status_code == 403


# --- authenticate ---

@pytest.mark.asyncio
async def test_authenticate_correct_credentials():
    user = _user(pw="secret123")
    repo = _FakeUserRepo({user.id: user})
    got = await authenticate(user.email, "secret123", repo)
    assert got.id == user.id


@pytest.mark.asyncio
async def test_authenticate_wrong_password_raises_401():
    user = _user(pw="secret123")
    repo = _FakeUserRepo({user.id: user})
    with pytest.raises(HTTPException) as exc:
        await authenticate(user.email, "wrong", repo)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_authenticate_unknown_email_raises_401():
    with pytest.raises(HTTPException) as exc:
        await authenticate("nope@x.com", "any", _FakeUserRepo({}))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_authenticate_inactive_user_raises_401():
    user = _user(active=False)
    repo = _FakeUserRepo({user.id: user})
    with pytest.raises(HTTPException) as exc:
        await authenticate(user.email, "secret123", repo)
    assert exc.value.status_code == 401
