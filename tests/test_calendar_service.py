"""Tests de calendar_service.

Estrategia (Directiva #4):
  - _overlaps_any / compute_free_slots: PURE (sin DB, sin mocks). Verifican el
    predicado de intervalos (borde excluido) y la generacion de slots.
  - is_slot_available / list_available_slots / next_available_slot: Tier B
    PostgreSQL real (auto-skip sin DB). Reusan AppointmentRepository.overlapping.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.calendar_service import (
    DEFAULT_STEP_MIN,
    _overlaps_any,
    compute_free_slots,
)

TZ = ZoneInfo("Europe/Madrid")


def _dt(h: int, m: int = 0, day: date = date(2026, 3, 2)) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


# --- _overlaps_any (puro) ---

def test_overlaps_true_when_intersecting():
    assert _overlaps_any(_dt(9, 30), _dt(10, 30), [(_dt(10, 0), _dt(11, 0))]) is True


def test_overlaps_false_back_to_back_border_excluded():
    # 9-10 termina justo cuando empieza 10-11 -> NO solapan (back-to-back permitido)
    assert _overlaps_any(_dt(9, 0), _dt(10, 0), [(_dt(10, 0), _dt(11, 0))]) is False


def test_overlaps_true_when_contained():
    assert _overlaps_any(_dt(9, 0), _dt(12, 0), [(_dt(10, 0), _dt(11, 0))]) is True


def test_overlaps_false_when_disjoint():
    assert _overlaps_any(_dt(9, 0), _dt(9, 30), [(_dt(10, 0), _dt(11, 0))]) is False


def test_overlaps_false_empty_busy():
    assert _overlaps_any(_dt(9, 0), _dt(10, 0), []) is False


# --- compute_free_slots (puro) ---

def test_free_slots_no_busy():
    windows = [(_dt(9, 0), _dt(12, 0))]
    slots = compute_free_slots(windows, [], duration_min=60, step_min=30)
    assert slots == [_dt(9, 0), _dt(9, 30), _dt(10, 0), _dt(10, 30), _dt(11, 0)]


def test_free_slots_with_busy_excludes_overlapping():
    windows = [(_dt(9, 0), _dt(12, 0))]
    busy = [(_dt(10, 0), _dt(11, 0))]
    slots = compute_free_slots(windows, busy, duration_min=60, step_min=30)
    # 9:00 (back-to-back con busy, libre), 9:30/10:00/10:30 (solapan), 11:00 (libre)
    assert slots == [_dt(9, 0), _dt(11, 0)]


def test_free_slots_multiple_windows_sorted():
    windows = [(_dt(16, 0), _dt(18, 0)), (_dt(9, 0), _dt(10, 0))]
    slots = compute_free_slots(windows, [], duration_min=60, step_min=60)
    # ventana tarde primero en la lista, pero el resultado va ordenado
    assert slots == [_dt(9, 0), _dt(16, 0), _dt(17, 0)]


def test_free_slots_duration_longer_than_window_empty():
    windows = [(_dt(9, 0), _dt(10, 0))]
    assert compute_free_slots(windows, [], duration_min=120, step_min=30) == []


def test_free_slots_empty_windows():
    assert compute_free_slots([], [(_dt(10, 0), _dt(11, 0))], duration_min=60) == []


def test_free_slots_invalid_duration_raises():
    with pytest.raises(ValueError):
        compute_free_slots([(_dt(9, 0), _dt(12, 0))], [], duration_min=0)


def test_default_step_is_sensible():
    assert DEFAULT_STEP_MIN in (10, 15, 30)


# --- Tier B: PostgreSQL real (auto-skip) ---

@pytest.mark.asyncio
async def test_list_available_slots_db(db_session):
    from app.db.models import (
        Appointment,
        AppointmentStatus,
        Availability,
        Service,
        Staff,
    )
    from app.db.repositories import (
        AppointmentRepository,
        AvailabilityRepository,
        ClientRepository,
        ServiceRepository,
        StaffRepository,
    )
    from app.services.calendar_service import CalendarService, _localize

    d = date(2026, 3, 2)
    staff = await StaffRepository(db_session).create({"name": "Luis", "is_active": True})
    service = await ServiceRepository(db_session).create(
        {"name": "Masaje", "duration_min": 60, "price": 50.0, "is_active": True}
    )
    await AvailabilityRepository(db_session).create({
        "staff_id": staff.id,
        "day_of_week": d.weekday(),
        "start_time": time(9, 0),
        "end_time": time(12, 0),
    })
    client = await ClientRepository(db_session).create({"name": "X", "phone": "+3400000001"})
    await AppointmentRepository(db_session).create({
        "client_id": client.id,
        "service_id": service.id,
        "staff_id": staff.id,
        "starts_at": _localize(d, time(10, 0)),
        "ends_at": _localize(d, time(11, 0)),
        "status": AppointmentStatus.CONFIRMED,
    })

    cal = CalendarService(db_session)
    slots = await cal.list_available_slots(staff.id, service.id, d, step_min=30)
    assert slots == [_localize(d, time(9, 0)), _localize(d, time(11, 0))]

    assert await cal.is_slot_available(staff.id, _localize(d, time(9, 0)), 60) is True
    assert await cal.is_slot_available(staff.id, _localize(d, time(10, 0)), 60) is False
    assert await cal.is_slot_available(staff.id, _localize(d, time(11, 0)), 60) is True


@pytest.mark.asyncio
async def test_next_available_slot_db(db_session):
    from app.db.models import Availability, Service, Staff
    from app.db.repositories import (
        AvailabilityRepository,
        ServiceRepository,
        StaffRepository,
    )
    from app.services.calendar_service import CalendarService, _localize

    d = date(2026, 3, 2)
    staff = await StaffRepository(db_session).create({"name": "Ana", "is_active": True})
    service = await ServiceRepository(db_session).create(
        {"name": "Facial", "duration_min": 30, "price": 40.0, "is_active": True}
    )
    await AvailabilityRepository(db_session).create({
        "staff_id": staff.id,
        "day_of_week": d.weekday(),
        "start_time": time(9, 0),
        "end_time": time(10, 0),
    })

    cal = CalendarService(db_session)
    nxt = await cal.next_available_slot(
        staff.id, service.id, _localize(d, time(8, 0))
    )
    assert nxt == _localize(d, time(9, 0))


@pytest.mark.asyncio
async def test_list_slots_empty_when_no_availability(db_session):
    from app.db.models import Service, Staff
    from app.db.repositories import ServiceRepository, StaffRepository
    from app.services.calendar_service import CalendarService

    staff = await StaffRepository(db_session).create({"name": "Nadie", "is_active": True})
    service = await ServiceRepository(db_session).create(
        {"name": "X", "duration_min": 60, "price": 10.0, "is_active": True}
    )
    cal = CalendarService(db_session)
    assert await cal.list_available_slots(staff.id, service.id, date(2026, 3, 3)) == []
