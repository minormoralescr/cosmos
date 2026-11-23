"""Tests de appointment_service + reminder_service.

Estrategia (Directiva #4):
  Tier A puro: compute_ends_at / compute_reminder_specs / _scheduled_at
    (sin DB, sin scheduler, sin mocks). Nucleo de las reglas de timing.
  Tier A glue: orquestacion con fake repos (frontera DB) + fake scheduler
    (frontera APScheduler). Los fakes son behavior-driven (devuelven datos
    realistas); se valida la DECISION del servicio (conflicto->raise, etc.),
    no se simula el exito.
  Tier B: integracion PostgreSQL real + fake scheduler (auto-skip sin DB).
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import (
    Appointment,
    AppointmentStatus,
    Client,
    Reminder,
    ReminderStatus,
    ReminderType,
    Service,
    Staff,
)
from app.services.appointment_service import (
    AppointmentConflictError,
    AppointmentService,
    compute_ends_at,
)
from app.services.reminder_service import (
    SEND_REMINDER_FUNC_REF,
    ReminderService,
    ReminderSpec,
    _scheduled_at,
    compute_reminder_specs,
)

UTC = timezone.utc
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _appt(starts_at: datetime, *, duration_h: float = 1.0, appt_id: int = 1) -> Appointment:
    return Appointment(
        id=appt_id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=duration_h),
    )


# ===== TIER A puro: compute_ends_at =====

def test_compute_ends_at_adds_duration():
    s = datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert compute_ends_at(s, 60) == datetime(2026, 1, 1, 11, tzinfo=UTC)
    assert compute_ends_at(s, 90) == datetime(2026, 1, 1, 11, 30, tzinfo=UTC)


def test_compute_ends_at_rejects_non_positive_duration():
    for bad in (0, -5):
        with pytest.raises(ValueError):
            compute_ends_at(NOW, bad)


# ===== TIER A puro: _scheduled_at =====

def test_scheduled_at_per_type():
    s = datetime(2026, 1, 2, 10, tzinfo=UTC)
    e = datetime(2026, 1, 2, 11, tzinfo=UTC)
    assert _scheduled_at(ReminderType.PRE_24H, s, e) == datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert _scheduled_at(ReminderType.POST_FEEDBACK, s, e) == datetime(2026, 1, 2, 13, tzinfo=UTC)
    assert _scheduled_at(ReminderType.EMAIL_BACKUP, s, e) == datetime(2026, 1, 2, 8, tzinfo=UTC)


# ===== TIER A puro: compute_reminder_specs =====

def test_specs_48h_away_with_email_yields_three_active():
    appt = _appt(NOW + timedelta(hours=48))  # empieza en 48h
    client = Client(email="ana@spa.com")
    specs = compute_reminder_specs(appt, client, NOW)
    by_type = {s.type: s for s in specs}
    assert set(by_type) == {ReminderType.PRE_24H, ReminderType.POST_FEEDBACK, ReminderType.EMAIL_BACKUP}
    assert all(not s.skip for s in specs)  # ninguno vencido
    # job_ids deterministicos
    assert by_type[ReminderType.PRE_24H].job_id == "appt-1-pre_24h"
    assert by_type[ReminderType.EMAIL_BACKUP].job_id == "appt-1-email_backup"


def test_specs_without_email_omits_email_backup():
    appt = _appt(NOW + timedelta(hours=48))
    client = Client(email=None)
    specs = compute_reminder_specs(appt, client, NOW)
    assert {s.type for s in specs} == {ReminderType.PRE_24H, ReminderType.POST_FEEDBACK}


def test_specs_client_none_omits_email_backup():
    appt = _appt(NOW + timedelta(hours=48))
    specs = compute_reminder_specs(appt, None, NOW)
    assert {s.type for s in specs} == {ReminderType.PRE_24H, ReminderType.POST_FEEDBACK}


def test_specs_1h_away_skips_pre24h_and_email_backup():
    """Cita en 1h: el 24h-record y el email-2h ya vencieron; solo post-feedback activo."""
    appt = _appt(NOW + timedelta(hours=1))  # empieza en 1h
    client = Client(email="ana@spa.com")
    specs = {s.type: s for s in compute_reminder_specs(appt, client, NOW)}
    assert specs[ReminderType.PRE_24H].skip is True        # 1h - 24h = pasado
    assert specs[ReminderType.EMAIL_BACKUP].skip is True   # 1h - 2h = pasado
    assert specs[ReminderType.POST_FEEDBACK].skip is False  # ends(1h)+1h+2h = futuro


def test_specs_3h_away_email_backup_still_active():
    """Cita en 3h: el 24h-record vencio, pero el email (2h antes) aun es futuro (1h desde ahora)."""
    appt = _appt(NOW + timedelta(hours=3))
    client = Client(email="ana@spa.com")
    specs = {s.type: s for s in compute_reminder_specs(appt, client, NOW)}
    assert specs[ReminderType.PRE_24H].skip is True
    assert specs[ReminderType.EMAIL_BACKUP].skip is False  # 3h - 2h = 1h futuro
    assert specs[ReminderType.POST_FEEDBACK].skip is False


def test_specs_past_appointment_all_skipped():
    appt = _appt(NOW - timedelta(hours=10))  # ya empezo y termino
    client = Client(email="ana@spa.com")
    specs = compute_reminder_specs(appt, client, NOW)
    assert all(s.skip for s in specs)


# ===== Fakes (fronteras DB + scheduler) =====

class _FakeScheduler:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.removed: list[str] = []

    async def add_job(self, func_ref, *, job_id, run_at, args=()):
        self.jobs[job_id] = {"func_ref": func_ref, "run_at": run_at, "args": args}

    async def remove_job(self, job_id):
        self.removed.append(job_id)
        existed = job_id in self.jobs
        self.jobs.pop(job_id, None)
        return existed


class _FakeReminderRepo:
    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.created: list[Reminder] = []
        self.updated: list[tuple] = []
        self._ids = itertools.count(1000)

    async def by_appointment(self, appt_id):
        return [r for r in self.existing if r.appointment_id == appt_id]

    async def create(self, data):
        r = Reminder(**data)
        r.id = next(self._ids)
        self.created.append(r)
        return r

    async def update(self, obj, data):
        for k, v in data.items():
            setattr(obj, k, v)
        self.updated.append((obj, dict(data)))
        return obj


class _FakeAppointmentRepo:
    def __init__(self, conflicts=None, existing=None):
        self.conflicts = list(conflicts or [])
        self.existing = existing
        self.created: list[Appointment] = []
        self.updated: list[tuple] = []
        self._ids = itertools.count(5000)

    async def overlapping(self, staff_id, starts_at, ends_at, *, exclude_id=None):
        return list(self.conflicts)

    async def create(self, data):
        a = Appointment(**data)
        a.id = next(self._ids)
        self.created.append(a)
        return a

    async def update(self, obj, data):
        for k, v in data.items():
            setattr(obj, k, v)
        self.updated.append((obj, dict(data)))
        return obj

    async def get(self, aid):
        return self.existing


class _FakeGetterRepo:
    """Repo minimal que solo implementa get (para services/staff/clients)."""
    def __init__(self, obj):
        self.obj = obj

    async def get(self, _id):
        return self.obj


def _reminder_svc(scheduler=None, reminder_repo=None) -> ReminderService:
    return ReminderService(
        session=None,
        scheduler=scheduler or _FakeScheduler(),
        reminders=reminder_repo or _FakeReminderRepo(),
    )


# ===== TIER A glue: schedule_reminders =====

@pytest.mark.asyncio
async def test_schedule_reminders_48h_email_creates_three_jobs():
    sched = _FakeScheduler()
    repo = _FakeReminderRepo()
    svc = _reminder_svc(sched, repo)
    appt = _appt(NOW + timedelta(hours=48), appt_id=7)
    client = Client(email="ana@spa.com")

    created = await svc.schedule_reminders(appt, client, now=NOW)

    assert len(created) == 3
    assert len(sched.jobs) == 3
    assert len(repo.created) == 3
    # job_ids deterministicos + func_ref correcto
    assert set(sched.jobs) == {
        "appt-7-pre_24h", "appt-7-post_feedback", "appt-7-email_backup"
    }
    for j in sched.jobs.values():
        assert j["func_ref"] == SEND_REMINDER_FUNC_REF
        assert len(j["args"]) == 1  # reminder_id


@pytest.mark.asyncio
async def test_schedule_reminders_1h_away_only_post_feedback():
    """Cita en 1h: 24h-record y email ya vencieron -> solo se agenda post-feedback."""
    sched = _FakeScheduler()
    svc = _reminder_svc(sched)
    appt = _appt(NOW + timedelta(hours=1), appt_id=8)
    client = Client(email="ana@spa.com")

    created = await svc.schedule_reminders(appt, client, now=NOW)

    assert len(created) == 1
    assert created[0].type == ReminderType.POST_FEEDBACK
    assert set(sched.jobs) == {"appt-8-post_feedback"}


@pytest.mark.asyncio
async def test_schedule_reminders_idempotent_cancels_previous_first():
    """Re-agendar: primero cancela los recordatorios pendientes previos."""
    sched = _FakeScheduler()
    prev = Reminder(
        id=999, appointment_id=5, type=ReminderType.PRE_24H,
        scheduled_at=NOW - timedelta(hours=1), status=ReminderStatus.PENDING,
        scheduler_job_id="appt-5-pre_24h",
    )
    repo = _FakeReminderRepo(existing=[prev])
    svc = _reminder_svc(sched, repo)

    await svc.schedule_reminders(_appt(NOW + timedelta(hours=48), appt_id=5), Client(email="x@y.com"), now=NOW)

    assert "appt-5-pre_24h" in sched.removed  # job previo removido
    assert prev.status == ReminderStatus.SKIPPED  # marcado skipped
    # y los nuevos se crearon
    assert any(r.type == ReminderType.PRE_24H and r.scheduler_job_id == "appt-5-pre_24h"
               for r in repo.created)


# ===== TIER A glue: cancel_reminders =====

@pytest.mark.asyncio
async def test_cancel_reminders_skips_only_pending_and_removes_jobs():
    sched = _FakeScheduler()
    sched.jobs["appt-1-pre_24h"] = {"func_ref": "x", "run_at": NOW, "args": ()}
    pending = Reminder(
        id=1, appointment_id=1, type=ReminderType.PRE_24H,
        scheduled_at=NOW + timedelta(hours=1), status=ReminderStatus.PENDING,
        scheduler_job_id="appt-1-pre_24h",
    )
    sent = Reminder(
        id=2, appointment_id=1, type=ReminderType.POST_FEEDBACK,
        scheduled_at=NOW + timedelta(hours=2), status=ReminderStatus.SENT,
        scheduler_job_id="appt-1-post_feedback",
    )
    repo = _FakeReminderRepo(existing=[pending, sent])
    svc = _reminder_svc(sched, repo)

    count = await svc.cancel_reminders(1)

    assert count == 1  # solo el pendiente
    assert pending.status == ReminderStatus.SKIPPED
    assert sent.status == ReminderStatus.SENT  # no tocado
    assert sched.removed == ["appt-1-pre_24h"]  # solo el job del pendiente


# ===== TIER A glue: AppointmentService.create_appointment =====

def _appt_svc(scheduler=None, *, service=None, staff=None, client=None,
              conflicts=None, appt_repo=None, reminder_repo=None) -> AppointmentService:
    reminder_svc = _reminder_svc(scheduler, reminder_repo)
    return AppointmentService(
        session=None,
        reminder_service=reminder_svc,
        appointments=appt_repo or _FakeAppointmentRepo(conflicts=conflicts),
        services=_FakeGetterRepo(service),
        staff=_FakeGetterRepo(staff),
        clients=_FakeGetterRepo(client),
    )


def _active_service(duration=60):
    return Service(id=1, name="Masaje", duration_min=duration, price=50, is_active=True)


def _active_staff():
    return Staff(id=2, name="Lua", is_active=True)


def _client_with_email():
    return Client(id=3, name="Ana", phone="+34600000001", email="ana@spa.com")


@pytest.mark.asyncio
async def test_create_appointment_success_confirmed_and_schedules():
    sched = _FakeScheduler()
    svc = _appt_svc(
        sched,
        service=_active_service(duration=60),
        staff=_active_staff(),
        client=_client_with_email(),
        conflicts=[],
    )
    starts = NOW + timedelta(hours=48)

    appt = await svc.create_appointment(
        client_id=3, service_id=1, staff_id=2, starts_at=starts, now=NOW,
    )

    assert appt.status == AppointmentStatus.CONFIRMED
    assert appt.ends_at == starts + timedelta(minutes=60)  # ends_at calculado
    assert appt.starts_at == starts
    # recordatorios agendados (48h + email -> 3)
    assert len(sched.jobs) == 3


@pytest.mark.asyncio
async def test_create_appointment_conflict_raises_and_does_not_create():
    sched = _FakeScheduler()
    conflict = Appointment(id=99, starts_at=NOW + timedelta(hours=48),
                           ends_at=NOW + timedelta(hours=49))
    appt_repo = _FakeAppointmentRepo(conflicts=[conflict])
    svc = _appt_svc(
        sched,
        service=_active_service(),
        staff=_active_staff(),
        client=_client_with_email(),
        appt_repo=appt_repo,
    )

    with pytest.raises(AppointmentConflictError) as exc:
        await svc.create_appointment(
            client_id=3, service_id=1, staff_id=2,
            starts_at=NOW + timedelta(hours=48), now=NOW,
        )
    assert exc.value.staff_id == 2
    assert len(appt_repo.created) == 0  # no se persistio la cita
    assert sched.jobs == {}  # no se agendo nada


@pytest.mark.asyncio
async def test_create_appointment_rejects_inactive_service():
    svc = _appt_svc(
        service=Service(id=1, duration_min=60, price=50, is_active=False),
        staff=_active_staff(), client=_client_with_email(), conflicts=[],
    )
    with pytest.raises(ValueError, match="inactivo"):
        await svc.create_appointment(
            client_id=3, service_id=1, staff_id=2,
            starts_at=NOW + timedelta(hours=48), now=NOW,
        )


@pytest.mark.asyncio
async def test_create_appointment_rejects_missing_staff():
    svc = _appt_svc(
        service=_active_service(), staff=None, client=_client_with_email(), conflicts=[],
    )
    with pytest.raises(ValueError, match="Terapeuta"):
        await svc.create_appointment(
            client_id=3, service_id=1, staff_id=2,
            starts_at=NOW + timedelta(hours=48), now=NOW,
        )


@pytest.mark.asyncio
async def test_create_appointment_rejects_past_start():
    svc = _appt_svc(
        service=_active_service(), staff=_active_staff(),
        client=_client_with_email(), conflicts=[],
    )
    with pytest.raises(ValueError, match="pasado"):
        await svc.create_appointment(
            client_id=3, service_id=1, staff_id=2,
            starts_at=NOW - timedelta(hours=1), now=NOW,
        )


# ===== TIER A glue: AppointmentService.cancel_appointment =====

@pytest.mark.asyncio
async def test_cancel_appointment_marks_cancelled_and_cancels_reminders():
    sched = _FakeScheduler()
    sched.jobs["appt-1-pre_24h"] = {"func_ref": "x", "run_at": NOW, "args": ()}
    pending = Reminder(
        id=1, appointment_id=1, type=ReminderType.PRE_24H,
        scheduled_at=NOW + timedelta(hours=1), status=ReminderStatus.PENDING,
        scheduler_job_id="appt-1-pre_24h",
    )
    reminder_repo = _FakeReminderRepo(existing=[pending])
    existing_appt = Appointment(
        id=1, client_id=3, service_id=1, staff_id=2,
        starts_at=NOW + timedelta(hours=48), ends_at=NOW + timedelta(hours=49),
        status=AppointmentStatus.CONFIRMED,
    )
    appt_repo = _FakeAppointmentRepo(existing=existing_appt)
    svc = _appt_svc(
        sched, service=_active_service(), staff=_active_staff(),
        client=_client_with_email(), appt_repo=appt_repo, reminder_repo=reminder_repo,
    )

    cancelled = await svc.cancel_appointment(1)

    assert cancelled.status == AppointmentStatus.CANCELLED
    assert sched.removed == ["appt-1-pre_24h"]  # job del recordatorio removido
    assert pending.status == ReminderStatus.SKIPPED


@pytest.mark.asyncio
async def test_cancel_appointment_already_cancelled_raises():
    appt = Appointment(
        id=1, starts_at=NOW + timedelta(hours=48), ends_at=NOW + timedelta(hours=49),
        status=AppointmentStatus.CANCELLED,
    )
    svc = _appt_svc(appt_repo=_FakeAppointmentRepo(existing=appt))
    with pytest.raises(ValueError, match="no se puede cancelar"):
        await svc.cancel_appointment(1)


@pytest.mark.asyncio
async def test_cancel_appointment_not_found_raises():
    svc = _appt_svc(appt_repo=_FakeAppointmentRepo(existing=None))
    with pytest.raises(ValueError, match="no existe"):
        await svc.cancel_appointment(999)


# ===== TIER B: integracion PostgreSQL real + fake scheduler (auto-skip) =====

@pytest.mark.asyncio
async def test_create_appointment_end_to_end_with_reminders(db_session):
    from app.db.repositories import (
        AppointmentRepository,
        ClientRepository,
        ReminderRepository,
        ServiceRepository,
        StaffRepository,
    )

    sched = _FakeScheduler()
    reminder_svc = ReminderService(db_session, sched)
    svc = AppointmentService(db_session, reminder_svc)

    service = await ServiceRepository(db_session).create(
        {"name": "Masaje", "duration_min": 60, "price": 50}
    )
    staff = await StaffRepository(db_session).create({"name": "Lua"})
    client = await ClientRepository(db_session).create(
        {"name": "Ana", "phone": "+34600000010", "email": "ana@spa.com"}
    )
    starts = datetime.now(UTC) + timedelta(hours=48)

    appt = await svc.create_appointment(
        client_id=client.id, service_id=service.id, staff_id=staff.id,
        starts_at=starts, now=datetime.now(UTC),
    )

    assert appt.status == AppointmentStatus.CONFIRMED
    assert appt.ends_at == starts + timedelta(minutes=60)
    # 3 recordatorios persistidos en DB + 3 jobs agendados
    reminders = await ReminderRepository(db_session).by_appointment(appt.id)
    assert len(reminders) == 3
    assert len(sched.jobs) == 3


@pytest.mark.asyncio
async def test_create_appointment_conflict_blocks_second(db_session):
    from app.db.repositories import (
        ClientRepository, ServiceRepository, StaffRepository,
    )

    svc = AppointmentService(
        db_session, ReminderService(db_session, _FakeScheduler())
    )
    service = await ServiceRepository(db_session).create(
        {"name": "Facial", "duration_min": 60, "price": 40}
    )
    staff = await StaffRepository(db_session).create({"name": "Mia"})
    client = await ClientRepository(db_session).create(
        {"name": "B", "phone": "+34600000011"}
    )
    starts = datetime.now(UTC) + timedelta(hours=48)
    await svc.create_appointment(
        client_id=client.id, service_id=service.id, staff_id=staff.id,
        starts_at=starts, now=datetime.now(UTC),
    )
    # segunda cita solapada (mismo staff, 30 min despues) -> conflicto
    with pytest.raises(AppointmentConflictError):
        await svc.create_appointment(
            client_id=client.id, service_id=service.id, staff_id=staff.id,
            starts_at=starts + timedelta(minutes=30), now=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_cancel_cascades_to_reminders(db_session):
    from app.db.repositories import (
        ClientRepository, ReminderRepository, ServiceRepository, StaffRepository,
    )

    sched = _FakeScheduler()
    svc = AppointmentService(db_session, ReminderService(db_session, sched))

    service = await ServiceRepository(db_session).create(
        {"name": "Masaje", "duration_min": 60, "price": 50}
    )
    staff = await StaffRepository(db_session).create({"name": "Lua"})
    client = await ClientRepository(db_session).create(
        {"name": "Ana", "phone": "+34600000020", "email": "ana@spa.com"}
    )
    starts = datetime.now(UTC) + timedelta(hours=48)

    appt = await svc.create_appointment(
        client_id=client.id, service_id=service.id, staff_id=staff.id,
        starts_at=starts, now=datetime.now(UTC),
    )
    # 3 recordatorios pendientes + 3 jobs tras crear
    reminders_before = await ReminderRepository(db_session).by_appointment(appt.id)
    assert len(reminders_before) == 3
    assert all(r.status == ReminderStatus.PENDING for r in reminders_before)
    assert len(sched.jobs) == 3

    cancelled = await svc.cancel_appointment(appt.id)
    assert cancelled.status == AppointmentStatus.CANCELLED

    reminders_after = await ReminderRepository(db_session).by_appointment(appt.id)
    assert all(r.status == ReminderStatus.SKIPPED for r in reminders_after)
    assert len(sched.removed) == 3  # los 3 jobs removidos
