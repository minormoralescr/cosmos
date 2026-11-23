"""Modelos de dominio (SQLAlchemy 2.0, estilo Mapped/mapped_column).

Esquema PostgreSQL real — sin sqlite, sin mocks. Cada status vive en un StrEnum
(single source of truth) y se persiste por VALOR (no por nombre) para legibilidad.

Entidades:
  Agent        agente humano (User) o IA (config en JSON)
  User         cuenta de la app (auth JWT + RBAC)
  Client       cliente del spa (telefono E.164)
  Service      servicio (duracion, precio)
  Staff        terapeuta que ejecuta el servicio
  Availability horario semanal recurrente por terapeuta (motor de slots, Fase 6)
  Appointment  cita (cliente + servicio + terapeuta + ventana temporal)
  Conversation hilo con un cliente (asignado a un agente o pool IA)
  Message      mensaje inbound/outbound por canal (whatsapp | email)
  Reminder     recordatorio programado (pre_24h | post_feedback | email_backup)
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Table,
    Text,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enum(enum_cls: type[StrEnum]) -> SAEnum:
    """SAEnum que persiste los VALORES del StrEnum (no los nombres). DRY."""
    return SAEnum(enum_cls, values_callable=lambda e: [m.value for m in e])


# --- Enumeraciones de dominio (single source of truth) ---

class AgentType(StrEnum):
    HUMAN = "human"
    AI = "ai"


class UserRole(StrEnum):
    ADMIN = "admin"
    RECEPCIONISTA = "recepcionista"


class ConversationStatus(StrEnum):
    OPEN = "open"
    AWAITING_HUMAN = "awaiting_human"
    CLOSED = "closed"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageChannel(StrEnum):
    WHATSAPP = "whatsapp"
    EMAIL = "email"


class AppointmentStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class ReminderType(StrEnum):
    PRE_24H = "pre_24h"
    POST_FEEDBACK = "post_feedback"
    EMAIL_BACKUP = "email_backup"


class ReminderStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


# --- Mixin compartido (DRY): timestamps en todas las entidades ---

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


# --- Asociacion M2M Staff <-> Service ---

staff_service = Table(
    "staff_service",
    Base.metadata,
    Column("staff_id", ForeignKey("staff.id", ondelete="CASCADE"), primary_key=True),
    Column("service_id", ForeignKey("service.id", ondelete="CASCADE"), primary_key=True),
)


# --- Modelos ---

class Agent(TimestampMixin, Base):
    __tablename__ = "agent"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[AgentType] = mapped_column(_enum(AgentType), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Solo para el agente IA: prompts, modelo, temperatura, etc.
    ai_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    user: Mapped[User | None] = relationship(back_populates="agent", uselist=False)
    conversations: Mapped[list[Conversation]] = relationship(back_populates="assigned_agent")


class User(TimestampMixin, Base):
    # "user" es palabra reservada en PostgreSQL -> tabla "user_account"
    __tablename__ = "user_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        _enum(UserRole), nullable=False, default=UserRole.RECEPCIONISTA
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agent.id"), nullable=True)

    agent: Mapped[Agent | None] = relationship(back_populates="user", uselist=False)


class Client(TimestampMixin, Base):
    __tablename__ = "client"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)  # E.164
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    appointments: Mapped[list[Appointment]] = relationship(back_populates="client")
    conversations: Mapped[list[Conversation]] = relationship(back_populates="client")


class Service(TimestampMixin, Base):
    __tablename__ = "service"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    staff: Mapped[list[Staff]] = relationship(
        secondary=staff_service, back_populates="services"
    )
    appointments: Mapped[list[Appointment]] = relationship(back_populates="service")


class Staff(TimestampMixin, Base):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    services: Mapped[list[Service]] = relationship(
        secondary=staff_service, back_populates="staff"
    )
    availability: Mapped[list[Availability]] = relationship(
        back_populates="staff", cascade="all, delete-orphan"
    )
    appointments: Mapped[list[Appointment]] = relationship(back_populates="staff")


class Availability(TimestampMixin, Base):
    __tablename__ = "availability"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(
        ForeignKey("staff.id", ondelete="CASCADE"), nullable=False
    )
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Lun .. 6=Dom (ISO)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)

    staff: Mapped[Staff] = relationship(back_populates="availability")


class Appointment(TimestampMixin, Base):
    __tablename__ = "appointment"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("client.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[int] = mapped_column(ForeignKey("service.id"), nullable=False)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[AppointmentStatus] = mapped_column(
        _enum(AppointmentStatus), nullable=False, default=AppointmentStatus.PENDING
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent.id"), nullable=True
    )

    client: Mapped[Client] = relationship(back_populates="appointments")
    service: Mapped[Service] = relationship(back_populates="appointments")
    staff: Mapped[Staff] = relationship(back_populates="appointments")
    reminders: Mapped[list[Reminder]] = relationship(
        back_populates="appointment", cascade="all, delete-orphan"
    )


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversation"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("client.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[ConversationStatus] = mapped_column(
        _enum(ConversationStatus), nullable=False, default=ConversationStatus.OPEN
    )
    assigned_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent.id"), nullable=True
    )  # NULL => atendido por pool IA
    last_intent: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # book | reschedule | cancel | faq | handoff
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    client: Mapped[Client] = relationship(back_populates="conversations")
    assigned_agent: Mapped[Agent | None] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(TimestampMixin, Base):
    __tablename__ = "message"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[MessageDirection] = mapped_column(
        _enum(MessageDirection), nullable=False
    )
    channel: Mapped[MessageChannel] = mapped_column(_enum(MessageChannel), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    twilio_message_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Reminder(TimestampMixin, Base):
    __tablename__ = "reminder"

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointment.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[ReminderType] = mapped_column(_enum(ReminderType), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[ReminderStatus] = mapped_column(
        _enum(ReminderStatus), nullable=False, default=ReminderStatus.PENDING
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # id del job en APScheduler: permite cancelarlo al cancelar la cita (Fase 4).
    scheduler_job_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    appointment: Mapped[Appointment] = relationship(back_populates="reminders")
