"""FastAPI app entrypoint.

Lifespan: arranca el scheduler (APScheduler + SQLAlchemyJobStore sobre Postgres)
y reagenda los Reminder PENDING futuros (recuperacion tras reinicio). Shutdown:
para el scheduler y dispose del engine.

Routers incluidos: webhooks (Twilio, firmado), auth (JWT), web (UI HTMX+Jinja).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from sqlalchemy import select

from app.api.auth import router as auth_router
from app.api.webhooks import router as webhook_router
from app.config import settings
from app.db.base import async_session_factory, engine
from app.db.models import Reminder, ReminderStatus
from app.scheduler.adapter import AsyncScheduler
from app.services.reminder_service import SEND_REMINDER_FUNC_REF
from app.web.routes import router as web_router

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

_scheduler: AsyncScheduler | None = None


async def _recover_pending_reminders(scheduler: AsyncScheduler) -> None:
    """Tras reinicio, reagenda los Reminder PENDING cuya hora aun no ha llegado.
    Los past-due se ignoran (perdieron su ventana); en una version futura podrian
    dispararse inmediatamente o marcarse skipped."""
    async with async_session_factory() as session:
        now = datetime.now(timezone.utc)
        stmt = select(Reminder).where(Reminder.status == ReminderStatus.PENDING)
        rems = list((await session.execute(stmt)).scalars().all())
    future = [r for r in rems if r.scheduled_at > now and r.scheduler_job_id]
    for rem in future:
        await scheduler.add_job(
            SEND_REMINDER_FUNC_REF,
            job_id=rem.scheduler_job_id,
            run_at=rem.scheduled_at,
            args=(rem.id,),
        )
    log.info(
        "scheduler: %d reminder(s) pendientes reagendados (%d past-due ignorados)",
        len(future), len(rems) - len(future),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    _scheduler = AsyncScheduler(database_url=settings.database_url)
    _scheduler.start()
    try:
        await _recover_pending_reminders(_scheduler)
    except Exception:
        log.exception("scheduler: fallo la recuperacion de reminders pendientes")
    yield
    if _scheduler is not None:
        _scheduler.shutdown()
    await engine.dispose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(auth_router)
app.include_router(web_router)


def get_scheduler() -> AsyncScheduler | None:
    """Expone el scheduler singleton (para inyectar donde se necesite)."""
    return _scheduler
