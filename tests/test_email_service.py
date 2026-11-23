"""Tests de email_service.

build_reminder_email: puro. send_email: frontera SMTP (aiosmtplib) mockeada via
monkeypatch (Directiva #4: la frontera externa se simula; la logica real).
"""
from __future__ import annotations

from datetime import datetime, timezone

import aiosmtplib
import pytest

from app.services.email_service import EmailService, build_reminder_email


def test_build_reminder_email_has_date_service_and_name():
    starts = datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)
    subject, body = build_reminder_email(starts, "Masaje", "Ana")
    assert "Recordatorio" in subject
    assert "Ana" in body
    assert "Masaje" in body
    assert "02/03/2026" in body


@pytest.mark.asyncio
async def test_send_email_calls_smtp_with_headers(monkeypatch):
    calls = []

    async def _fake_send(msg, **kwargs):
        calls.append((msg, kwargs))

    monkeypatch.setattr(aiosmtplib, "send", _fake_send)
    svc = EmailService(host="smtp.test", port=587, from_addr="spa@test", user="u", password="p")
    await svc.send_email("cliente@x.com", "Hola", "Cuerpo")
    assert len(calls) == 1
    msg = calls[0][0]
    assert msg["To"] == "cliente@x.com"
    assert msg["Subject"] == "Hola"
    assert msg["From"] == "spa@test"
    assert calls[0][1]["hostname"] == "smtp.test"


@pytest.mark.asyncio
async def test_send_email_raises_if_not_configured():
    svc = EmailService(host="", from_addr="")  # sin SMTP
    with pytest.raises(RuntimeError):
        await svc.send_email("c@x.com", "s", "b")
