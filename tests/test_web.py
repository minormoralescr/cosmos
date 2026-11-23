"""Tests de la capa web.

Estrategia (Directiva #4):
  - session cookie helpers: puros (itsdangerous real, sin app).
  - render de templates: smoke Jinja2 (sin app, sin DB) -> detecta errores de
    sintaxis y variables faltantes en las plantillas mas usadas.
  - Tier B: flujo login -> dashboard con app FastAPI real + DB (auto-skip).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.web import session as web_session
from app.web.routes import templates


# --- session cookie helpers (puros) ---

def test_session_cookie_roundtrip():
    token = web_session.create_session_cookie(42)
    assert web_session.read_session_cookie(token) == 42


def test_session_cookie_tampered_returns_none():
    token = web_session.create_session_cookie(1)
    # alterar un caracter del payload -> firma invalida
    tampered = token[:-2] + ("aa" if token[-2:] != "aa" else "bb")
    assert web_session.read_session_cookie(tampered) is None


def test_session_cookie_garbage_returns_none():
    assert web_session.read_session_cookie("not-a-token") is None
    assert web_session.read_session_cookie(None) is None
    assert web_session.read_session_cookie("") is None


def test_session_cookie_wrong_salt_returns_none():
    # cookie firmada con otro salt -> no valida con el salt de la app
    from itsdangerous import URLSafeTimedSerializer
    from app.config import settings
    other = URLSafeTimedSerializer(settings.jwt_secret, salt="otro-salt")
    assert web_session.read_session_cookie(other.dumps({"uid": 1})) is None


# --- render de templates (smoke Jinja2, sin app) ---

def _fake_user(role="admin"):
    return SimpleNamespace(
        full_name="Ana Ruiz", role=SimpleNamespace(value=role), agent_id=None,
    )


def test_render_login_template():
    out = templates.env.get_template("login.html").render({"user": None})
    assert "Iniciar sesión" in out
    assert 'name="email"' in out


def test_render_dashboard_template():
    out = templates.env.get_template("dashboard.html").render({
        "user": _fake_user(), "awaiting": 3, "upcoming": [], "now": datetime.now(timezone.utc),
    })
    assert "Panel" in out
    assert "conversaciones esperando operador" in out


def test_render_appointments_template_empty():
    out = templates.env.get_template("appointments.html").render({
        "user": _fake_user(), "appointments": [],
    })
    assert "Sin citas próximas" in out


def test_render_inbox_template_empty():
    out = templates.env.get_template("inbox.html").render({
        "user": _fake_user(), "conversations": [],
    })
    assert "Sin conversaciones pendientes" in out


def test_render_calendar_template():
    staff = [SimpleNamespace(id=1, name="Luis")]
    services = [SimpleNamespace(id=1, name="Masaje", duration_min=60)]
    out = templates.env.get_template("calendar.html").render({
        "user": _fake_user(), "staff_list": staff, "services": services,
    })
    assert "Reservar cita" in out
    assert "Luis" in out


def test_render_messages_partial():
    m = SimpleNamespace(
        body="Hola", direction=SimpleNamespace(value="inbound"),
        created_at=datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc),
    )
    out = templates.env.get_template("_messages.html").render({"messages": [m]})
    assert "Hola" in out


def test_render_slots_partial_with_slots():
    s = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
    out = templates.env.get_template("_slots.html").render({
        "slots": [s], "staff_id": 1, "service_id": 1,
        "client_phone": "+346", "client_name": "Ana", "day": "2026-03-02",
    })
    assert "09:00" in out
    assert 'name="starts_at"' in out


def test_render_agents_template():
    agents = [SimpleNamespace(id=1, name="Glam AI", type=SimpleNamespace(value="ai"), is_active=True)]
    out = templates.env.get_template("agents.html").render({
        "user": _fake_user(), "agents": agents, "ai_agent": agents[0],
    })
    assert "Glam AI" in out
    assert "(IA actual)" in out


# --- Tier B: flujo login -> dashboard con app real (auto-skip) ---

@pytest.mark.asyncio
async def test_web_login_flow_db(db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.auth.security import hash_password
    from app.db.base import get_session
    from app.db.models import User, UserRole
    from app.db.repositories import UserRepository
    from app.web.routes import router as web_router

    await UserRepository(db_session).create({
        "email": "op@spa.test", "password_hash": hash_password("secret123"),
        "full_name": "Operador", "role": UserRole.RECEPCIONISTA, "is_active": True,
    })
    await db_session.commit()

    app = FastAPI()
    app.include_router(web_router)

    # Override get_session: nueva sesion del factory en el loop de TestClient
    # (los datos commiteados por db_session son visibles a la nueva sesion).
    from app.db.base import async_session_factory, get_session
    async def _override_session():
        async with async_session_factory() as s:
            yield s
    app.dependency_overrides[get_session] = _override_session

    client = TestClient(app)
    r = client.post("/login", data={"email": "op@spa.test", "password": "secret123"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    cookies = r.headers.get("set-cookie", "")
    assert web_session.COOKIE_NAME in cookies

    r2 = client.get("/dashboard", follow_redirects=False)
    assert r2.status_code == 200
    assert "Panel" in r2.text
