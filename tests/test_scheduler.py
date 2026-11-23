"""Tests del scheduler adapter (APScheduler).

Tier A con use_memory=True (MemoryJobStore, sin Postgres): add_job/remove_job/
resolve. El jobstore real (SQLAlchemyJobStore) se prueba en Tier B (auto-skip).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.scheduler.adapter import AsyncScheduler

FUNC_REF = "app.jobs.reminder_jobs:send_reminder"
FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


def test_resolve_func_ref():
    func = AsyncScheduler._resolve(FUNC_REF)
    assert callable(func)
    assert func.__name__ == "send_reminder"


@pytest.mark.asyncio
async def test_add_job_schedules():
    sched = AsyncScheduler(use_memory=True)
    await sched.add_job(FUNC_REF, job_id="j1", run_at=FUTURE, args=(1,))
    assert sched.get_job("j1") is not None


@pytest.mark.asyncio
async def test_add_job_replace_existing_no_duplicate():
    sched = AsyncScheduler(use_memory=True)
    await sched.add_job(FUNC_REF, job_id="j1", run_at=FUTURE, args=(1,))
    await sched.add_job(FUNC_REF, job_id="j1", run_at=FUTURE, args=(2,))
    # sigue habiendo un solo job con ese id (replace_existing)
    assert sched.get_job("j1") is not None


@pytest.mark.asyncio
async def test_remove_job_existing_and_missing():
    sched = AsyncScheduler(use_memory=True)
    await sched.add_job(FUNC_REF, job_id="j1", run_at=FUTURE, args=(1,))
    assert await sched.remove_job("j1") is True
    assert await sched.remove_job("j1") is False  # ya no existe
    assert await sched.remove_job("nunca") is False


@pytest.mark.asyncio
async def test_implements_scheduler_port():
    """El adapter satisface SchedulerPort (add_job/remove_job async)."""
    sched = AsyncScheduler(use_memory=True)
    assert hasattr(sched, "add_job") and hasattr(sched, "remove_job")
    await sched.add_job(FUNC_REF, job_id="ok", run_at=FUTURE, args=(1,))
    assert sched.get_job("ok") is not None
