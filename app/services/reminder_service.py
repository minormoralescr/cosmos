"""Reminder service — orquesta creacion + scheduling de recordatorios por cita.

Reglas de negocio (logica pura en compute_reminder_specs, testeable sin DB):
  - PRE_24H   (WhatsApp): starts_at - 24h
  - POST_FEEDBACK (WhatsApp): ends_at + 2h
  - EMAIL_BACKUP (email): starts_at - 2h, SOLO si el cliente tiene email
  - Skip: si scheduled_at <= now (ya paso la hora) no se agenda

Scheduler = frontera inyectable (SchedulerPort); la impl real de APScheduler va
en Fase 9. El job target es una referencia textual ('app.jobs.reminder_jobs:send_reminder')
compatible con el SQLAlchemyJobStore de APScheduler (persiste jobs como texto ->
sobreviven reinicios).

Mocks: SOLO scheduler (frontera externa APScheduler) y repos (frontera DB),
ambos inyectables. La logica de timing y reglas es real y pura.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Appointment, Client, Reminder, ReminderStatus, ReminderType
from app.db.repositories import ReminderRepository

# Referencia textual al job (APScheduler la resuelve por import en Fase 9).
# String, no import -> el modulo app.jobs.reminder_jobs se crea en Fase 9 sin
# romper este modulo hoy.
SEND_REMINDER_FUNC_REF = "app.jobs.reminder_jobs:send_reminder"


class SchedulerPort(Protocol):
    """Frontera del scheduler (APScheduler real en Fase 9; fake en tests)."""

    async def add_job(
        self, func_ref: str, *, job_id: str, run_at: datetime, args: tuple = ()
    ) -> None: ...

    async def remove_job(self, job_id: str) -> bool: ...


class NullScheduler:
    """SchedulerPort no-op: los Reminder se persisten (PENDING) pero no se
    registran jobs. Usado mientras el APScheduler real no esta wired (Fase 8 web);
    la Fase 9 inyecta el adapter real, que puede registrar jobs para los PENDING
    existentes al arrancar."""

    async def add_job(self, func_ref: str, *, job_id: str, run_at: datetime, args: tuple = ()) -> None:
        return None

    async def remove_job(self, job_id: str) -> bool:
        return False


# --- Logica pura (sin DB, sin scheduler) ---

@dataclass(frozen=True)
class ReminderSpec:
    """Especificacion de un recordatorio: tipo, hora, job_id, y si ya es tarde."""
    type: ReminderType
    scheduled_at: datetime
    job_id: str
    skip: bool  # True si scheduled_at <= now (ya paso -> no agendar)


def _scheduled_at(
    rtype: ReminderType, starts_at: datetime, ends_at: datetime
) -> datetime:
    """Hora a la que debe dispararse cada tipo de recordatorio."""
    if rtype == ReminderType.PRE_24H:
        return starts_at - timedelta(hours=24)
    if rtype == ReminderType.POST_FEEDBACK:
        return ends_at + timedelta(hours=2)
    if rtype == ReminderType.EMAIL_BACKUP:
        return starts_at - timedelta(hours=2)
    raise ValueError(f"Tipo de recordatorio no soportado: {rtype}")


def compute_reminder_specs(
    appt: Appointment, client: Client | None, now: datetime
) -> list[ReminderSpec]:
    """Que recordatorios crear, a que hora, y si ya es tarde (skip).

    Nucleo puro de las reglas: sin DB, sin scheduler, testeable directamente.
    """
    types = [ReminderType.PRE_24H, ReminderType.POST_FEEDBACK]
    if client is not None and client.email:
        types.append(ReminderType.EMAIL_BACKUP)
    specs: list[ReminderSpec] = []
    for rtype in types:
        scheduled = _scheduled_at(rtype, appt.starts_at, appt.ends_at)
        specs.append(ReminderSpec(
            type=rtype,
            scheduled_at=scheduled,
            job_id=f"appt-{appt.id}-{rtype.value}",
            skip=scheduled <= now,
        ))
    return specs


# --- Servicio ---

class ReminderService:
    def __init__(
        self,
        session: AsyncSession,
        scheduler: SchedulerPort,
        *,
        reminders: ReminderRepository | None = None,
    ) -> None:
        self.session = session
        self.scheduler = scheduler
        self.reminders = reminders or ReminderRepository(session)

    async def schedule_reminders(
        self, appt: Appointment, client: Client | None, *, now: datetime | None = None
    ) -> list[Reminder]:
        """Crea filas Reminder (PENDING) + registra jobs para las no-skipped.

        Idempotente: cancela recordatorios pendientes previos antes de crear
        (seguro ante re-agendado de la cita).
        """
        now = now or datetime.now(timezone.utc)
        await self.cancel_reminders(appt.id)  # idempotencia
        created: list[Reminder] = []
        for spec in compute_reminder_specs(appt, client, now):
            if spec.skip:
                continue
            reminder = await self.reminders.create({
                "appointment_id": appt.id,
                "type": spec.type,
                "scheduled_at": spec.scheduled_at,
                "status": ReminderStatus.PENDING,
                "scheduler_job_id": spec.job_id,
            })
            await self.scheduler.add_job(
                SEND_REMINDER_FUNC_REF,
                job_id=spec.job_id,
                run_at=spec.scheduled_at,
                args=(reminder.id,),
            )
            created.append(reminder)
        return created

    async def cancel_reminders(self, appointment_id: int) -> int:
        """Marca pendientes como SKIPPED + remueve sus jobs. Devuelve cuantos cancelo.

        No toca los SENT/FAILED/SKIPPED (ya se procesaron o ya se cancelaron).
        """
        existing = await self.reminders.by_appointment(appointment_id)
        cancelled = 0
        for r in existing:
            if r.status != ReminderStatus.PENDING:
                continue
            if r.scheduler_job_id:
                await self.scheduler.remove_job(r.scheduler_job_id)
            await self.reminders.update(r, {"status": ReminderStatus.SKIPPED})
            cancelled += 1
        return cancelled
