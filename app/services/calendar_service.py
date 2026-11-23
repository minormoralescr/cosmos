"""Calendar service — motor de slots basado en disponibilidad local.

Google Calendar sync OFF (feature flag google_sync_enabled=False, MVP). El motor
usa solo la DB local: Availability (ventana por dia de semana) + Appointments
(ocupados, estados CONFIRMED/PENDING). La interfaz se diseña para anadir un
adapter de Google despues sin tocar el calendario interno.

Pure (Tier A, sin DB):
  - _overlaps_any(start, end, busy) -> bool   (predicado de intervalos, borde
    excluido; espejo del SQL de AppointmentRepository.overlapping).
  - compute_free_slots(windows, busy, duration_min, step_min) -> list[datetime]
    Para cada ventana, avanza de step_min en step_min; incluye el slot si cabe
    en la ventana (slot_end <= window_end) y no solapa ninguna cita ocupada.

DB (Tier B): is_slot_available / list_available_slots / next_available_slot.
Reutilizan AppointmentRepository.overlapping (no duplican el predicado en SQL).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories import (
    AppointmentRepository,
    AvailabilityRepository,
    ServiceRepository,
)
from app.services.appointment_service import compute_ends_at

DEFAULT_STEP_MIN = 15  # granularidad de slots (cada 15 min)
SEARCH_HORIZON_DAYS = 30  # busqueda de next_available_slot

_tz = ZoneInfo(settings.business_tz)


def _localize(day: date, t: time) -> datetime:
    """Combina fecha + hora naive de Availability -> datetime aware en business_tz."""
    return datetime.combine(day, t, tzinfo=_tz)


def _overlaps_any(
    start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]
) -> bool:
    """True si [start, end) solapa algun intervalo ocupado. Borde excluido:
    dos citas back-to-back (fin == inicio) NO solapan. Espejo del SQL de
    AppointmentRepository.overlapping (start1 < end2 AND end1 > start2)."""
    return any(start < b_end and b_start < end for b_start, b_end in busy)


def compute_free_slots(
    windows: list[tuple[datetime, datetime]],
    busy: list[tuple[datetime, datetime]],
    duration_min: int,
    step_min: int = DEFAULT_STEP_MIN,
) -> list[datetime]:
    """Pure: slots libres dados ventanas + ocupados + duracion + step.

    Para cada ventana avanza el cursor de step_min en step_min desde window_start;
    incluye el slot [cursor, cursor+duration] si cabe en la ventana y no solapa
    ningun intervalo ocupado. Devuelve la lista ordenada.
    """
    if duration_min <= 0 or step_min <= 0:
        raise ValueError("duration_min y step_min deben ser > 0")
    free: list[datetime] = []
    for w_start, w_end in windows:
        cursor = w_start
        while cursor + timedelta(minutes=duration_min) <= w_end:
            slot_end = cursor + timedelta(minutes=duration_min)
            if not _overlaps_any(cursor, slot_end, busy):
                free.append(cursor)
            cursor += timedelta(minutes=step_min)
    return sorted(free)


class CalendarService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        appointments: AppointmentRepository | None = None,
        availability: AvailabilityRepository | None = None,
        services: ServiceRepository | None = None,
    ) -> None:
        self.session = session
        self.appointments = appointments or AppointmentRepository(session)
        self.availability = availability or AvailabilityRepository(session)
        self.services = services or ServiceRepository(session)

    async def is_slot_available(
        self, staff_id: int, starts_at: datetime, duration_min: int
    ) -> bool:
        """True si el slot [starts_at, starts_at+duration) no choca con citas
        CONFIRMED/PENDING del terapeuta. Reusa overlapping (frontera DB)."""
        ends_at = compute_ends_at(starts_at, duration_min)
        conflicts = await self.appointments.overlapping(staff_id, starts_at, ends_at)
        return len(conflicts) == 0

    async def list_available_slots(
        self,
        staff_id: int,
        service_id: int,
        day: date,
        *,
        step_min: int = DEFAULT_STEP_MIN,
    ) -> list[datetime]:
        """Slots libres para un terapeuta + servicio en una fecha.

        1. servicio activo? -> duration_min
        2. ventanas de Availability para ese dia de la semana (date.weekday(),
           0=Lun..6=Dom, ISO == Availability.day_of_week)
        3. ocupados del dia = overlapping(day_start, day_end) (CONFIRMED/PENDING)
        4. compute_free_slots (pure)
        """
        service = await self.services.get(service_id)
        if service is None or not service.is_active:
            return []
        windows_db = await self.availability.list_for_staff_and_day(
            staff_id, day.weekday()
        )
        if not windows_db:
            return []
        windows = [
            (_localize(day, w.start_time), _localize(day, w.end_time))
            for w in windows_db
        ]
        day_start = _localize(day, time(0, 0))
        day_end = day_start + timedelta(days=1)
        busy_appts = await self.appointments.overlapping(staff_id, day_start, day_end)
        busy = [(a.starts_at, a.ends_at) for a in busy_appts]
        return compute_free_slots(windows, busy, service.duration_min, step_min)

    async def next_available_slot(
        self,
        staff_id: int,
        service_id: int,
        from_dt: datetime,
        *,
        step_min: int = DEFAULT_STEP_MIN,
    ) -> datetime | None:
        """Primer slot libre >= from_dt, buscando hasta SEARCH_HORIZON_DAYS dias."""
        if from_dt.tzinfo is None:
            from_dt = from_dt.replace(tzinfo=_tz)
        base = from_dt.astimezone(_tz).date()
        for i in range(SEARCH_HORIZON_DAYS):
            day = base + timedelta(days=i)
            slots = await self.list_available_slots(
                staff_id, service_id, day, step_min=step_min
            )
            future = [s for s in slots if s >= from_dt]
            if future:
                return future[0]
        return None


# Dependency de FastAPI
def get_calendar_service(session: AsyncSession) -> CalendarService:
    return CalendarService(session)
