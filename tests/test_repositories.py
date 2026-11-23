"""Tests de repositories — dos tiers:

Tier A (sin DB): valida que cada repo construye la query correcta contra el
  dialecto PostgreSQL, usando una sesion "capturadora" que registra el statement.
  El mock vive SOLO en la frontera de la sesion (captura SQL), nunca en logica
  de negocio (Directiva #4 compliant).

Tier B (PostgreSQL real): valida comportamiento CRUD + casos de borde.
  Auto-skip si no hay Postgres alcanzable (conftest.db_session).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects import postgresql

from app.db.base import Base
from app.db.models import (
    Agent,
    Appointment,
    AppointmentStatus,
    Client,
    Conversation,
    Message,
    Reminder,
    ReminderStatus,
    Service,
    Staff,
    User,
)
from app.db.repositories import (
    AgentRepository,
    AppointmentRepository,
    BaseRepository,
    ClientRepository,
    ConversationRepository,
    MessageRepository,
    ReminderRepository,
    ServiceRepository,
    StaffRepository,
    UserRepository,
)


# --- Capturador de sesion (frontera; NO mockea logica) ---

class _Scalars:
    def __init__(self, rows): self._rows = list(rows)
    def all(self): return list(self._rows)
    def scalar_one(self): return self._rows[0] if self._rows else None
    def scalar_one_or_none(self): return self._rows[0] if self._rows else None
    def first(self): return self._rows[0] if self._rows else None


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = len(self._rows)
    def scalars(self): return _Scalars(self._rows)
    def scalar_one(self): return self._rows[0] if self._rows else None
    def scalar_one_or_none(self): return self._rows[0] if self._rows else None


class _FakeSession:
    """Registra statements + llamadas de mutacion. No ejecuta nada real."""
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.last_stmt = None
        self.added: list = []
        self.deleted: list = []
        self.flushed = 0
        self.refreshed: list = []
    async def execute(self, stmt, *a, **kw):
        self.last_stmt = stmt
        return _Result(self.rows)
    def add(self, obj):
        self.added.append(obj)
    async def flush(self):
        self.flushed += 1
    async def refresh(self, obj):
        self.refreshed.append(obj)
    async def delete(self, obj):
        self.deleted.append(obj)
    async def get(self, model, id):
        return None


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# ===== TIER A: sin DB =====

REPO_MODEL_PAIRS = [
    (UserRepository, User),
    (AgentRepository, Agent),
    (ClientRepository, Client),
    (ServiceRepository, Service),
    (StaffRepository, Staff),
    (AppointmentRepository, Appointment),
    (ConversationRepository, Conversation),
    (MessageRepository, Message),
    (ReminderRepository, Reminder),
]


def test_repo_model_bindings():
    for repo_cls, model_cls in REPO_MODEL_PAIRS:
        assert repo_cls.model is model_cls, f"{repo_cls.__name__} mal bindeado"


def test_pk_inspection_single_int_pk():
    for repo_cls, _ in REPO_MODEL_PAIRS:
        pk = sa_inspect(repo_cls.model).primary_key
        assert len(pk) == 1, f"{repo_cls.__name__}: PK compuesta no soportada"
        assert pk[0].name == "id", f"{repo_cls.__name__}: PK no es 'id'"


def test_as_dict_passes_plain_dict():
    assert BaseRepository._as_dict({"x": 1, "y": 2}) == {"x": 1, "y": 2}


def test_as_dict_pydantic_exclude_unset():
    from pydantic import BaseModel

    class Patch(BaseModel):
        name: str | None = None
        phone: str | None = None

    assert BaseRepository._as_dict(Patch(name="Ana")) == {"name": "Ana"}


@pytest.mark.asyncio
async def test_get_by_email_query_targets_user_account():
    fake = _FakeSession()
    await UserRepository(fake).get_by_email("ana@spa.com")
    sql = _compile(fake.last_stmt)
    assert "user_account" in sql and "email" in sql and "ana@spa.com" in sql


@pytest.mark.asyncio
async def test_get_ai_agent_query_filters_type_and_active():
    fake = _FakeSession()
    await AgentRepository(fake).get_ai_agent()
    sql = _compile(fake.last_stmt)
    assert "agent" in sql and "type" in sql and "is_active" in sql


@pytest.mark.asyncio
async def test_overlapping_query_uses_interval_predicate():
    fake = _FakeSession()
    s = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    e = datetime(2026, 1, 1, 11, tzinfo=timezone.utc)
    await AppointmentRepository(fake).overlapping(staff_id=1, starts_at=s, ends_at=e)
    sql = _compile(fake.last_stmt)
    assert "appointment" in sql
    assert "staff_id" in sql and "starts_at" in sql and "ends_at" in sql
    assert "status" in sql  # filtro de estados validos


@pytest.mark.asyncio
async def test_pending_due_query_filters_status_and_time():
    fake = _FakeSession()
    await ReminderRepository(fake).pending_due(datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    sql = _compile(fake.last_stmt)
    assert "reminder" in sql and "scheduled_at" in sql and "status" in sql


@pytest.mark.asyncio
async def test_list_awaiting_human_orders_by_updated_desc():
    fake = _FakeSession()
    await ConversationRepository(fake).list_awaiting_human()
    sql = _compile(fake.last_stmt)
    assert "conversation" in sql and "updated_at" in sql


@pytest.mark.asyncio
async def test_list_for_service_uses_m2m_exists():
    fake = _FakeSession()
    await StaffRepository(fake).list_for_service(service_id=7)
    sql = _compile(fake.last_stmt)
    assert "staff" in sql and "service" in sql  # JOIN/EXISTS sobre staff_service


# --- Mecanica CRUD de BaseRepository (sin DB; via sesion capturadora) ---

@pytest.mark.asyncio
async def test_create_instantiates_model_and_delegates_to_session():
    fake = _FakeSession()
    repo = ClientRepository(fake)
    obj = await repo.create({"name": "Ana", "phone": "+34001"})
    assert isinstance(obj, Client)
    assert obj.name == "Ana" and obj.phone == "+34001"
    assert fake.added == [obj]      # session.add llamado con la instancia
    assert fake.flushed == 1        # flush (persistir y obtener PK)
    assert fake.refreshed == [obj]  # refresh (cargar defaults/server defaults)


@pytest.mark.asyncio
async def test_update_applies_setattr_only_for_provided_keys():
    fake = _FakeSession()
    repo = ClientRepository(fake)
    existing = Client(name="Old", phone="+34001")
    updated = await repo.update(existing, {"name": "New"})
    assert updated.name == "New"
    assert updated.phone == "+34001"  # clave no provista: NO se modifica
    assert fake.flushed == 1 and fake.refreshed == [existing]


@pytest.mark.asyncio
async def test_delete_by_id_builds_filtered_delete_stmt():
    fake = _FakeSession(rows=[1])  # rowcount=1 -> True
    repo = ClientRepository(fake)
    ok = await repo.delete_by_id(42)
    assert ok is True
    sql = _compile(fake.last_stmt)
    assert "DELETE FROM client" in sql and "id" in sql and "42" in sql


# ===== TIER B: integracion PostgreSQL real (auto-skip) =====

@pytest.mark.asyncio
async def test_crud_client_roundtrip(db_session):
    repo = ClientRepository(db_session)
    created = await repo.create({"name": "Ana Lopez", "phone": "+34600000001"})
    assert created.id is not None
    fetched = await repo.get(created.id)
    assert fetched is not None and fetched.phone == "+34600000001"
    await repo.update(fetched, {"name": "Ana M."})
    assert fetched.name == "Ana M."
    assert await repo.count() == 1
    assert await repo.delete_by_id(created.id) is True
    assert await repo.get(created.id) is None


@pytest.mark.asyncio
async def test_get_or_create_by_phone_is_idempotent(db_session):
    repo = ClientRepository(db_session)
    c1 = await repo.get_or_create_by_phone("+34600000002", name="Beto")
    c2 = await repo.get_or_create_by_phone("+34600000002", name="Beto X")
    assert c1.id == c2.id
    assert c2.name == "Beto"  # NO se sobrescribe si ya existe


@pytest.mark.asyncio
async def test_overlapping_detects_conflict_and_borde_excluido(db_session):
    svc = await ServiceRepository(db_session).create(
        {"name": "Masaje", "duration_min": 60, "price": 50}
    )
    staff = await StaffRepository(db_session).create({"name": "Lua"})
    client = await ClientRepository(db_session).create(
        {"name": "C", "phone": "+34600000003"}
    )
    s1 = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    e1 = datetime(2026, 1, 1, 11, tzinfo=timezone.utc)
    repo = AppointmentRepository(db_session)
    appt = await repo.create({
        "client_id": client.id, "service_id": svc.id, "staff_id": staff.id,
        "starts_at": s1, "ends_at": e1, "status": AppointmentStatus.CONFIRMED,
    })

    # solapamiento real [10:30, 11:30] -> 1 conflicto
    conflicts = await repo.overlapping(
        staff.id,
        datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 11, 30, tzinfo=timezone.utc),
    )
    assert len(conflicts) == 1

    # excluir la propia cita al reagendar -> 0
    self_excluded = await repo.overlapping(staff.id, s1, e1, exclude_id=appt.id)
    assert len(self_excluded) == 0

    # borde tocando [11:00, 12:00] -> NO solapa (back-to-back permitido)
    no_conflict = await repo.overlapping(
        staff.id,
        datetime(2026, 1, 1, 11, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
    )
    assert len(no_conflict) == 0


@pytest.mark.asyncio
async def test_pending_due_filters_by_time_and_status(db_session):
    svc = await ServiceRepository(db_session).create(
        {"name": "Facial", "duration_min": 45, "price": 40}
    )
    staff = await StaffRepository(db_session).create({"name": "Mia"})
    client = await ClientRepository(db_session).create(
        {"name": "D", "phone": "+34600000004"}
    )
    appt = await AppointmentRepository(db_session).create({
        "client_id": client.id, "service_id": svc.id, "staff_id": staff.id,
        "starts_at": datetime(2026, 1, 1, 10, tzinfo=timezone.utc),
        "ends_at": datetime(2026, 1, 1, 11, tzinfo=timezone.utc),
        "status": AppointmentStatus.CONFIRMED,
    })
    repo = ReminderRepository(db_session)
    now = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
    await repo.create({
        "appointment_id": appt.id, "type": "pre_24h",
        "scheduled_at": datetime(2025, 12, 31, 10, tzinfo=timezone.utc),
        "status": ReminderStatus.PENDING,  # vencido + pendiente -> due
    })
    await repo.create({
        "appointment_id": appt.id, "type": "post_feedback",
        "scheduled_at": datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        "status": ReminderStatus.PENDING,  # futuro -> no due
    })
    await repo.create({
        "appointment_id": appt.id, "type": "email_backup",
        "scheduled_at": datetime(2025, 12, 31, 10, tzinfo=timezone.utc),
        "status": ReminderStatus.SENT,  # vencido pero ya enviado -> no due
    })

    due = await repo.pending_due(now)
    assert len(due) == 1
    assert due[0].type.value == "pre_24h"
