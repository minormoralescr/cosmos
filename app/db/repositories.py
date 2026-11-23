"""Repositories async (DRY): una base CRUD generica + subclases finas por entidad.

Toda operacion CRUD (get/list/count/create/update/delete) vive en BaseRepository.
Las subclases solo fijan `model` y anaden lookups de dominio que las fases
siguientes consumiran. No hay CRUD duplicado por entidad (Directiva #1).

Supuesto: cada modelo tiene una PK simple de una columna (cierto en todo el esquema).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.base import Base
from app.db.models import (
    Agent,
    AgentType,
    Appointment,
    AppointmentStatus,
    Availability,
    Client,
    Conversation,
    ConversationStatus,
    Message,
    Reminder,
    ReminderStatus,
    Service,
    Staff,
    User,
)

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """CRUD async generico. Las subclases fijan `model`."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- PK inspeccionada (no hardcodeada) ---
    @property
    def pk_column(self):
        return inspect(self.model).primary_key[0]

    async def get(self, id: Any) -> ModelT | None:
        return await self.session.get(self.model, id)

    async def get_multi(
        self, *, skip: int = 0, limit: int = 100, order_by: Any | None = None
    ) -> list[ModelT]:
        stmt = (
            select(self.model)
            .order_by(order_by or self.pk_column)
            .offset(skip)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count(self) -> int:
        stmt = select(func.count()).select_from(self.model)
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    async def create(self, data: dict[str, Any] | BaseModel) -> ModelT:
        obj = self.model(**self._as_dict(data))
        self.session.add(obj)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def update(self, obj: ModelT, data: dict[str, Any] | BaseModel) -> ModelT:
        for key, value in self._as_dict(data).items():
            setattr(obj, key, value)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def delete(self, obj: ModelT) -> None:
        await self.session.delete(obj)
        await self.session.flush()

    async def delete_by_id(self, id: Any) -> bool:
        stmt = sa_delete(self.model).where(self.pk_column == id)
        result = await self.session.execute(stmt)
        return (result.rowcount or 0) > 0

    @staticmethod
    def _as_dict(data: dict[str, Any] | BaseModel) -> dict[str, Any]:
        """Acepta dict o pydantic BaseModel (exclude_unset -> updates parciales)."""
        if isinstance(data, BaseModel):
            return data.model_dump(exclude_unset=True)
        return dict(data)


# --- Subclases finas: solo `model` + lookups de dominio ---


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        return (await self.session.execute(stmt)).scalar_one_or_none()


class AgentRepository(BaseRepository[Agent]):
    model = Agent

    async def get_ai_agent(self) -> Agent | None:
        """Singleton del agente IA activo (config en ai_config)."""
        stmt = select(Agent).where(
            Agent.type == AgentType.AI, Agent.is_active.is_(True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_human_agents(self) -> list[Agent]:
        stmt = (
            select(Agent)
            .where(Agent.type == AgentType.HUMAN, Agent.is_active.is_(True))
            .order_by(Agent.name)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class ClientRepository(BaseRepository[Client]):
    model = Client

    async def get_by_phone(self, phone: str) -> Client | None:
        stmt = select(Client).where(Client.phone == phone)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_create_by_phone(
        self, phone: str, *, name: str | None = None
    ) -> Client:
        """Idempotente: si el cliente existe NO se sobrescribe (preserva datos)."""
        client = await self.get_by_phone(phone)
        if client is None:
            client = await self.create({"phone": phone, "name": name or phone})
        return client


class ServiceRepository(BaseRepository[Service]):
    model = Service

    async def list_active(self) -> list[Service]:
        stmt = select(Service).where(Service.is_active.is_(True)).order_by(Service.name)
        return list((await self.session.execute(stmt)).scalars().all())


class StaffRepository(BaseRepository[Staff]):
    model = Staff

    async def list_active(self) -> list[Staff]:
        stmt = select(Staff).where(Staff.is_active.is_(True)).order_by(Staff.name)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_service(self, service_id: int) -> list[Staff]:
        """Terapeutas activos que ofrecen un servicio (via M2M staff_service)."""
        stmt = (
            select(Staff)
            .where(Staff.is_active.is_(True))
            .where(Staff.services.any(Service.id == service_id))
            .order_by(Staff.name)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class AvailabilityRepository(BaseRepository[Availability]):
    model = Availability

    async def list_for_staff_and_day(
        self, staff_id: int, day_of_week: int
    ) -> list[Availability]:
        """Ventanas de disponibilidad de un terapeuta para un dia de la semana
        (0=Lun .. 6=Dom, ISO, igual que Availability.day_of_week). Ordenadas por
        hora de inicio; puede haber varias ventanas (manana + tarde)."""
        stmt = (
            select(Availability)
            .where(
                Availability.staff_id == staff_id,
                Availability.day_of_week == day_of_week,
            )
            .order_by(Availability.start_time)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class AppointmentRepository(BaseRepository[Appointment]):
    model = Appointment

    async def overlapping(
        self,
        staff_id: int,
        starts_at: datetime,
        ends_at: datetime,
        *,
        exclude_id: int | None = None,
    ) -> list[Appointment]:
        """Citas confirmadas/pending que se solapan con [starts_at, ends_at).

        Predicado de solapamiento de intervalos: start1 < end2 AND end1 > start2.
        Borde excluido: dos citas que terminan/empiezan a la misma hora NO
        solapan (permite back-to-back). Usado por el slot engine (Fase 6) y
        appointment_service (Fase 4).
        """
        stmt = select(Appointment).where(
            Appointment.staff_id == staff_id,
            Appointment.status.in_(
                [AppointmentStatus.CONFIRMED, AppointmentStatus.PENDING]
            ),
            Appointment.starts_at < ends_at,
            Appointment.ends_at > starts_at,
        )
        if exclude_id is not None:
            stmt = stmt.where(Appointment.id != exclude_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def upcoming_for_client(
        self, client_id: int, *, now: datetime, limit: int = 10
    ) -> list[Appointment]:
        stmt = (
            select(Appointment)
            .where(
                Appointment.client_id == client_id,
                Appointment.starts_at >= now,
                Appointment.status.in_(
                    [AppointmentStatus.CONFIRMED, AppointmentStatus.PENDING]
                ),
            )
            .order_by(Appointment.starts_at)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_upcoming(
        self, *, now: datetime, limit: int = 50
    ) -> list[Appointment]:
        """Proximas citas CONFIRMED/PENDING con cliente/terapeuta/servicio cargados
        (selectinload) para la vista de agenda. Ordenadas por starts_at."""
        stmt = (
            select(Appointment)
            .options(
                selectinload(Appointment.client),
                selectinload(Appointment.staff),
                selectinload(Appointment.service),
            )
            .where(
                Appointment.starts_at >= now,
                Appointment.status.in_(
                    [AppointmentStatus.CONFIRMED, AppointmentStatus.PENDING]
                ),
            )
            .order_by(Appointment.starts_at)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class ConversationRepository(BaseRepository[Conversation]):
    model = Conversation

    async def get_open_by_client(self, client_id: int) -> Conversation | None:
        stmt = select(Conversation).where(
            Conversation.client_id == client_id,
            Conversation.status == ConversationStatus.OPEN,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_awaiting_human(self) -> list[Conversation]:
        """Bandeja de derivacion: conversaciones que el agente IA paso a humano.
        Carga el cliente (selectinload) para evitar lazy load async en la vista."""
        stmt = (
            select(Conversation)
            .options(selectinload(Conversation.client))
            .where(Conversation.status == ConversationStatus.AWAITING_HUMAN)
            .order_by(Conversation.updated_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())


class MessageRepository(BaseRepository[Message]):
    model = Message

    async def list_by_conversation(self, conversation_id: int) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class ReminderRepository(BaseRepository[Reminder]):
    model = Reminder

    async def pending_due(self, now: datetime) -> list[Reminder]:
        """Recordatorios pendientes cuya hora de envio ya llego (scheduler poll)."""
        stmt = (
            select(Reminder)
            .where(
                Reminder.status == ReminderStatus.PENDING,
                Reminder.scheduled_at <= now,
            )
            .order_by(Reminder.scheduled_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def by_appointment(self, appointment_id: int) -> list[Reminder]:
        stmt = select(Reminder).where(Reminder.appointment_id == appointment_id)
        return list((await self.session.execute(stmt)).scalars().all())
