"""Email backup service — envio async via SMTP (aiosmtplib).

Canal de respaldo para recordatorios EMAIL_BACKUP (clientes con email) y para
post-feedback por email. Frontera externa: servidor SMTP (aiosmtplib real; fake
en tests via monkeypatch de aiosmtplib.send).

build_reminder_email(...): puro (subject + body en español).
"""
from __future__ import annotations

from email.mime.text import MIMEText

import aiosmtplib

from app.config import settings


def build_reminder_email(
    starts_at, service_name: str, client_name: str, *, spa_name: str = "Spa"
) -> tuple[str, str]:
    """Pure: subject + body del email de recordatorio de cita."""
    when = starts_at.strftime("%d/%m/%Y a las %H:%M")
    subject = f"Recordatorio de tu cita - {spa_name}"
    body = (
        f"Hola {client_name},\n\n"
        f"Te recordamos tu cita para {service_name} el {when}.\n\n"
        f"Si necesitas reprogramar, respondes a este correo o por WhatsApp.\n\n"
        f"¡Te esperamos!\n\n{spa_name}"
    )
    return subject, body


class EmailService:
    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        from_addr: str | None = None,
    ) -> None:
        self._host = host if host is not None else settings.smtp_host
        self._port = port if port is not None else settings.smtp_port
        self._user = user if user is not None else settings.smtp_user
        self._password = password if password is not None else settings.smtp_password
        self._from = from_addr if from_addr is not None else settings.smtp_from

    async def send_email(self, to: str, subject: str, body: str) -> None:
        """Envía un email de texto plano. Lanza RuntimeError si SMTP no configurado."""
        if not self._host or not self._from:
            raise RuntimeError("SMTP no configurado: set SMTP_HOST y SMTP_FROM")
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = self._from
        msg["To"] = to
        msg["Subject"] = subject
        await aiosmtplib.send(
            msg,
            hostname=self._host,
            port=self._port,
            username=self._user or None,
            password=self._password or None,
            start_tls=self._port != 465,
        )


def get_email_service() -> EmailService:
    return EmailService()
