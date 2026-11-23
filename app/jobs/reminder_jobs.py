"""Job de recordatorio — target de SEND_REMINDER_FUNC_REF.

send_reminder(reminder_id): ejecutado por APScheduler en su propia sesion (no
request-scoped). Carga el reminder + cita + cliente + servicio, envía por el
canal adecuado (WhatsApp / email) y marca el Reminder SENT/FAILED.

dispatch_reminder(...): logica de envio testeable (fronteras whatsapp/email
inyectables; sin DB).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.db.base import async_session_factory
from app.db.models import ReminderStatus
from app.db.repositories import (
    AppointmentRepository,
    ClientRepository,
    ReminderRepository,
    ServiceRepository,
)
from app.services.email_service import EmailService, build_reminder_email
from app.services.whatsapp_service import WhatsAppService

log = logging.getLogger(__name__)


def _reminder_text(rtype: str, appt, service_name: str) -> str:
    when = appt.starts_at.strftime("%d/%m/%Y a las %H:%M")
    if rtype == "pre_24h":
        return f"🧖 Recordatorio: tu cita para {service_name} es el {when}. ¡Te esperamos!"
    if rtype == "post_feedback":
        return f"¡Gracias por tu visita! ¿Cómo estuvo tu cita de {service_name}? Valóranos del 1 al 5 ⭐"
    return f"Recordatorio de cita para {service_name} el {when}."


async def dispatch_reminder(rem, appt, client, service, whatsapp, email) -> None:
    """Envía el recordatorio por el canal adecuado. Fronteras inyectables.
    No-op si el cliente no tiene canal para ese tipo."""
    service_name = service.name if service else "tu servicio"
    rtype = rem.type.value
    if rtype == "email_backup":
        if not client or not client.email:
            return
        subject, body = build_reminder_email(appt.starts_at, service_name, client.name)
        await email.send_email(client.email, subject, body)
        return
    # pre_24h / post_feedback -> WhatsApp
    if not client or not client.phone:
        return
    await whatsapp.send_whatsapp(client.phone, _reminder_text(rtype, appt, service_name))


async def send_reminder(
    reminder_id: int, *, whatsapp: object | None = None, email: object | None = None
) -> None:
    """Job target: carga, envía, marca SENT (o FAILED con error). Idempotente:
    si el reminder ya no esta PENDING, no hace nada.

    whatsapp/email inyectables para tests (default: servicios reales). APScheduler
    llama send_reminder(reminder_id) -> usa los defaults."""
    async with async_session_factory() as session:
        rem_repo = ReminderRepository(session)
        rem = await rem_repo.get(reminder_id)
        if rem is None or rem.status != ReminderStatus.PENDING:
            return
        appt = await AppointmentRepository(session).get(rem.appointment_id)
        if appt is None:
            return
        client = await ClientRepository(session).get(appt.client_id)
        service = await ServiceRepository(session).get(appt.service_id)
        try:
            await dispatch_reminder(
                rem, appt, client, service,
                whatsapp or WhatsAppService(), email or EmailService(),
            )
            await rem_repo.update(
                rem, {"status": ReminderStatus.SENT, "sent_at": datetime.now(timezone.utc)}
            )
            await session.commit()
        except Exception as exc:
            log.exception("reminder %s fallo", reminder_id)
            await rem_repo.update(
                rem, {"status": ReminderStatus.FAILED, "error": str(exc)[:500]}
            )
            await session.commit()
