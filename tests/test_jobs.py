"""Tests de los jobs de recordatorios.

_reminder_text / dispatch_reminder: testeables con fakes (fronteras whatsapp/email
inyectables; modelos SimpleNamespace). send_reminder (wrapper con sesion real)
es Tier B (auto-skip).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.jobs.reminder_jobs import _reminder_text, dispatch_reminder


def _rem(rtype):
    return SimpleNamespace(type=SimpleNamespace(value=rtype))


def _appt():
    return SimpleNamespace(starts_at=datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc))


def _client(phone="+34600000001", email=None):
    return SimpleNamespace(phone=phone, email=email, name="Ana")


def _svc():
    return SimpleNamespace(name="Masaje")


class _FakeWA:
    def __init__(self):
        self.sent = []

    async def send_whatsapp(self, to, body):
        self.sent.append((to, body))


class _FakeEmail:
    def __init__(self):
        self.sent = []

    async def send_email(self, to, subject, body):
        self.sent.append((to, subject, body))


# --- _reminder_text ---

def test_reminder_text_pre_24h():
    t = _reminder_text("pre_24h", _appt(), "Masaje")
    assert "Masaje" in t and "02/03/2026" in t


def test_reminder_text_post_feedback():
    t = _reminder_text("post_feedback", _appt(), "Facial")
    assert "1 al 5" in t


# --- dispatch_reminder ---

@pytest.mark.asyncio
async def test_dispatch_pre_24h_sends_whatsapp():
    wa, em = _FakeWA(), _FakeEmail()
    await dispatch_reminder(_rem("pre_24h"), _appt(), _client(), _svc(), wa, em)
    assert len(wa.sent) == 1 and em.sent == []
    assert "Masaje" in wa.sent[0][1]


@pytest.mark.asyncio
async def test_dispatch_post_feedback_sends_whatsapp():
    wa = _FakeWA()
    await dispatch_reminder(_rem("post_feedback"), _appt(), _client(), _svc(), wa, _FakeEmail())
    assert "1 al 5" in wa.sent[0][1]


@pytest.mark.asyncio
async def test_dispatch_email_backup_sends_email():
    wa, em = _FakeWA(), _FakeEmail()
    await dispatch_reminder(
        _rem("email_backup"), _appt(), _client(phone=None, email="c@x.com"), _svc(), wa, em
    )
    assert len(em.sent) == 1 and wa.sent == []
    assert "Masaje" in em.sent[0][2]  # body


@pytest.mark.asyncio
async def test_dispatch_email_backup_without_email_is_noop():
    wa, em = _FakeWA(), _FakeEmail()
    await dispatch_reminder(
        _rem("email_backup"), _appt(), _client(phone=None, email=None), _svc(), wa, em
    )
    assert wa.sent == [] and em.sent == []


@pytest.mark.asyncio
async def test_dispatch_whatsapp_type_without_phone_is_noop():
    wa = _FakeWA()
    await dispatch_reminder(
        _rem("pre_24h"), _appt(), _client(phone=None, email="c@x.com"), _svc(), wa, _FakeEmail()
    )
    assert wa.sent == []


# --- send_reminder (Tier B, auto-skip) ---

@pytest.mark.asyncio
async def test_send_reminder_marks_sent_db(db_session):
    from app.db.models import (
        Agent,
        AgentType,
        Appointment,
        AppointmentStatus,
        Client,
        Reminder,
        ReminderStatus,
        ReminderType,
        Service,
        Staff,
    )
    from app.db.repositories import (
        AppointmentRepository,
        ClientRepository,
        ReminderRepository,
        ServiceRepository,
        StaffRepository,
    )
    from app.jobs.reminder_jobs import send_reminder

    # send_reminder usa async_session_factory global; este test verifica la
    # logica con la sesion de test inyectando via monkeypatch del factory.
    from app.db import base as db_base

    client = await ClientRepository(db_session).create({"name": "Ana", "phone": "+340001"})
    staff = await StaffRepository(db_session).create({"name": "Luis", "is_active": True})
    service = await ServiceRepository(db_session).create(
        {"name": "Masaje", "duration_min": 60, "price": 50.0, "is_active": True}
    )
    appt = await AppointmentRepository(db_session).create({
        "client_id": client.id, "service_id": service.id, "staff_id": staff.id,
        "starts_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
        "ends_at": datetime(2099, 1, 1, 1, tzinfo=timezone.utc),
        "status": AppointmentStatus.CONFIRMED,
    })
    rem = await ReminderRepository(db_session).create({
        "appointment_id": appt.id, "type": ReminderType.PRE_24H,
        "scheduled_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
        "status": ReminderStatus.PENDING, "scheduler_job_id": "test-j1",
    })
    await db_session.commit()

    # Override del factory para usar la sesion de test; inyectamos whatsapp/email
    # fakes -> dispatch tiene exito -> el job marca SENT.
    import app.jobs.reminder_jobs as jobs_mod
    orig_factory = db_base.async_session_factory

    async def _factory():
        class _Wrapper:
            def __init__(self, s): self._s = s
            async def __aenter__(self): return self._s
            async def __aexit__(self, *a): pass
        return _Wrapper(db_session)

    jobs_mod.async_session_factory = _factory
    try:
        await send_reminder(rem.id, whatsapp=_FakeWA(), email=_FakeEmail())
    finally:
        jobs_mod.async_session_factory = orig_factory

    refreshed = await ReminderRepository(db_session).get(rem.id)
    assert refreshed.status == ReminderStatus.SENT
    assert refreshed.sent_at is not None
