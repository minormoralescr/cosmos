"""APScheduler adapter — SchedulerPort sobre SQLAlchemyJobStore (Postgres).

AsyncIOScheduler + jobstore sync (psycopg2) en la misma DB: los jobs persisten y
sobreviven a reinicios. run_at tz-aware (business_tz).

Para tests: use_memory=True -> MemoryJobStore (sin Postgres), permite probar
add_job/remove_job/resolve sin DB (Tier A).
"""
from __future__ import annotations

import importlib
from datetime import datetime

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from app.config import settings
from app.services.reminder_service import SchedulerPort


def _sync_url(database_url: str) -> str:
    """asyncpg (async) -> psycopg2 (sync) para el jobstore de APScheduler."""
    return database_url.replace("+asyncpg", "+psycopg2")


class AsyncScheduler(SchedulerPort):
    """Implementacion real de SchedulerPort con APScheduler."""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        timezone: str | None = None,
        use_memory: bool = False,
    ) -> None:
        tz = timezone or settings.business_tz
        if use_memory or not database_url:
            jobstores = {"default": MemoryJobStore()}
        else:
            jobstores = {"default": SQLAlchemyJobStore(url=_sync_url(database_url))}
        self._sched = AsyncIOScheduler(jobstores=jobstores, timezone=tz)
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._sched.start()
            self._started = True

    def shutdown(self, *, wait: bool = False) -> None:
        if self._started:
            self._sched.shutdown(wait=wait)
            self._started = False

    async def add_job(
        self, func_ref: str, *, job_id: str, run_at: datetime, args: tuple = ()
    ) -> None:
        func = self._resolve(func_ref)
        self._sched.add_job(
            func,
            DateTrigger(run_date=run_at),
            id=job_id,
            args=tuple(args),
            replace_existing=True,
        )

    async def remove_job(self, job_id: str) -> bool:
        try:
            self._sched.remove_job(job_id)
            return True
        except Exception:
            return False

    def get_job(self, job_id: str):
        return self._sched.get_job(job_id)

    @staticmethod
    def _resolve(func_ref: str):
        """'app.jobs.reminder_jobs:send_reminder' -> callable."""
        module_path, attr = func_ref.split(":")
        return getattr(importlib.import_module(module_path), attr)
