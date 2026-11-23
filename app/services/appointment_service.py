"""Appointment service — orquesta creacion/cancelacion de citas + recordatorios.

Logica de negocio:
  - create_appointment: valida servicio/terapeuta/cliente activos, calcula ends_at,
    detecta conflictos de horario (reutiliza AppointmentRepository.overlapping),
    persiste la cita CONFIRMADA y dispara el scheduling de recordatorios.
  - cancel_appointment: marca CANCELADA y cancela recordatorios pendientes.

Pure function compute_ends_at testeable sin DB. El servicio depende de
ReminderService (inyectable) y repos (inyectables, frontera DB).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Appointment, AppointmentStatus
from app.db.repositories import (
    AppointmentRepository,
    ClientRepository,
    ServiceRepository,
    StaffRepository,
)
from app.services.reminder_service import ReminderService


class AppointmentConflictError(Exception):
    """Conflicto de horario: el terapeuta ya tiene cita que se solapa."""

    def __init__(self, staff_id: int, starts_at: datetime, ends_at: datetime, conflicts):
        self.staff_id = staff_id
        self.starts_at = starts_at
        self.ends_at = ends_at
        self.conflicts = list(conflicts)
        super().__init__(
            f"Conflicto de horario para staff_id={staff_id} "
            f"en [{starts_at}, {ends_at}): {len(self.conflicts)} cita(s) en conflicto"
        )


def compute_ends_at(starts_at: datetime, duration_min: int) -> datetime:
    """Pure: ends_at = starts_at + duracion del servicio (en minutos)."""
    if duration_min <= 0:
        raise ValueError(f"Duracion invalida: {duration_min} min (debe ser > 0)")
    return starts_at + timedelta(minutes=duration_min)


class AppointmentService:
    def __init__(
        self,
        session: AsyncSession,
        reminder_service: ReminderService,
        *,
        appointments: AppointmentRepository | None = None,
        services: ServiceRepository | None = None,
        clients: ClientRepository | None = None,
        staff: StaffRepository | None = None,
    ) -> None:
        self.session = session
        self.reminder_service = reminder_service
        self.appointments = appointments or AppointmentRepository(session)
        self.services = services or ServiceRepository(session)
        self.clients = clients or ClientRepository(session)
        self.staff = staff or StaffRepository(session)

    async def create_appointment(
        self,
        *,
        client_id: int,
        service_id: int,
        staff_id: int,
        starts_at: datetime,
        created_by_agent_id: int | None = None,
        now: datetime | None = None,
    ) -> Appointment:
        """Crea una cita CONFIRMADA + agenda sus recordatorios.

        Orden: valida todas las entradas primero (fail-fast), luego calcula
        ends_at, detecta conflictos y persiste. Si algo falla, no se crea nada
        (la transaccion de la peticion hace rollback).
        """
        now = now or datetime.now(timezone.utc)
        if starts_at < now:
            raise ValueError("La cita no puede empezar en el pasado")

        service = await self.services.get(service_id)
        if service is None or not service.is_active:
            raise ValueError(f"Servicio {service_id} no existe o esta inactivo")

        staff = await self.staff.get(staff_id)
        if staff is None or not staff.is_active:
            raise ValueError(f"Terapeuta {staff_id} no existe o esta inactivo")

        client = await self.clients.get(client_id)
        if client is None:
            raise ValueError(f"Cliente {client_id} no existe")

        ends_at = compute_ends_at(starts_at, service.duration_min)

        conflicts = await self.appointments.overlapping(staff_id, starts_at, ends_at)
        if conflicts:
            raise AppointmentConflictError(staff_id, starts_at, ends_at, conflicts)

        appt = await self.appointments.create({
            "client_id": client_id,
            "service_id": service_id,
            "staff_id": staff_id,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "status": AppointmentStatus.CONFIRMED,
            "created_by_agent_id": created_by_agent_id,
        })
        await self.reminder_service.schedule_reminders(appt, client, now=now)
        return appt

    async def cancel_appointment(self, appointment_id: int) -> Appointment:
        """Marca la cita CANCELADA y cancela sus recordatorios pendientes."""
        appt = await self.appointments.get(appointment_id)
        if appt is None:
            raise ValueError(f"Cita {appointment_id} no existe")
        if appt.status in (AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED):
            raise ValueError(
                f"Cita {appointment_id} no se puede cancelar (estado={appt.status.value})"
            )
        await self.appointments.update(appt, {"status": AppointmentStatus.CANCELLED})
        await self.reminder_service.cancel_reminders(appointment_id)
        return appt
