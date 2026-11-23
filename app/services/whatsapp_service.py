"""WhatsApp service — punto unico de contacto con Twilio (DRY).

Responsabilidades:
  - envio: texto libre (ventana 24h) / plantilla content_sid (fuera de ventana,
    para recordatorios)
  - verificacion de firma del webhook: HMAC-SHA1 sobre (uri + params) con
    AuthToken como clave, comparacion anti-timing con hmac.compare_digest
  - normalizacion + validacion E.164 (el canal WA lleva prefijo 'whatsapp:')

El cliente Twilio y el RequestValidator son lazy (una sola instancia por servicio).
Las llamadas sync del SDK se delegan a asyncio.to_thread para no bloquear el loop.

Mocks: SOLO en la frontera (Client inyectable). La logica de normalizacion,
validacion E.164 y decision de firma es real y testeada con HMAC computado.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import re
from typing import Any

from pydantic import BaseModel
from twilio.rest import Client
from twilio.request_validator import RequestValidator

from app.config import settings

_WHATSAPP_PREFIX = "whatsapp:"
_E164_RE = re.compile(r"^\+\d{6,15}$")


# --- Funciones puras (sin Twilio, sin red — testeables directamente) ---

def normalize_phone(value: str) -> str:
    """Quita el prefijo 'whatsapp:' que Twilio prepende en el canal WA."""
    if isinstance(value, str) and value.startswith(_WHATSAPP_PREFIX):
        return value[len(_WHATSAPP_PREFIX):]
    return value


def validate_e164(phone: str) -> None:
    """E.164: '+' seguido de 6-15 digitos. ValueError si invalido."""
    if not isinstance(phone, str) or not _E164_RE.match(phone):
        raise ValueError(f"Telefono no es E.164 valido: {phone!r}")


def _wa_address(phone: str) -> str:
    """Direccion de canal WhatsApp para ENVIO: whatsapp:+E164.

    Twilio exige el prefijo 'whatsapp:' en From/To para rutear al canal WA
    (sin el prefijo rutearia a SMS). En DB guardamos E.164 puro (unicidad),
    y re-anadimos el prefijo solo al enviar.
    """
    return _WHATSAPP_PREFIX + phone


class InboundMessage(BaseModel):
    """DTO normalizado de un mensaje inbound de Twilio."""
    from_number: str
    to_number: str
    body: str
    message_sid: str
    profile_name: str | None = None
    raw: dict[str, Any]


# --- Servicio ---

class WhatsAppService:
    """Single source of truth para Twilio. Fronteras inyectables para tests."""

    def __init__(
        self,
        client: Client | None = None,
        auth_token: str | None = None,
    ) -> None:
        self._client = client
        # auth_token drives both the guard (fail-closed) y el validator (HMAC key).
        self._auth_token = (
            auth_token if auth_token is not None else settings.twilio_auth_token
        )
        self._validator: RequestValidator | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            if not settings.twilio_account_sid or not settings.twilio_auth_token:
                raise RuntimeError(
                    "Twilio no configurado: set TWILIO_ACCOUNT_SID y TWILIO_AUTH_TOKEN"
                )
            self._client = Client(
                settings.twilio_account_sid, settings.twilio_auth_token
            )
        return self._client

    @property
    def validator(self) -> RequestValidator:
        if self._validator is None:
            self._validator = RequestValidator(self._auth_token)
        return self._validator

    async def send_whatsapp(self, to: str, body: str) -> str:
        """Texto libre. Solo valido en la ventana de 24h tras inbound del usuario.

        Twilio rechaza fuera de ventana -> usar send_template para recordatorios.
        """
        if not body or not body.strip():
            raise ValueError("body no puede estar vacio")
        to = normalize_phone(to)
        validate_e164(to)
        msg = await asyncio.to_thread(
            self.client.messages.create,
            from_=settings.twilio_whatsapp_from,
            to=_wa_address(to),
            body=body,
        )
        return msg.sid

    async def send_template(
        self,
        to: str,
        content_sid: str,
        variables: dict[str, str] | None = None,
    ) -> str:
        """Plantilla aprobada (content_sid). Fuera de ventana — para recordatorios.

        `variables` mapea placeholders {{1}} -> valor; se serializa a JSON
        en `content_variables` como exige la Twilio Content API.
        """
        if not content_sid:
            raise ValueError("content_sid no puede estar vacio")
        to = normalize_phone(to)
        validate_e164(to)
        kwargs: dict[str, Any] = {
            "from_": settings.twilio_whatsapp_from,
            "to": _wa_address(to),
            "content_sid": content_sid,
        }
        if variables:
            kwargs["content_variables"] = json.dumps(variables)
        msg = await asyncio.to_thread(self.client.messages.create, **kwargs)
        return msg.sid

    def verify_webhook_signature(
        self, *, uri: str, params: dict[str, Any], signature: str
    ) -> bool:
        """HMAC-SHA1 sobre (uri + params) con AuthToken como clave.

        Fail-closed: signature ausente o AuthToken vacio -> False (nunca True).
        Anti-timing: hmac.compare_digest (comparacion de tiempo constante).

        Seguridad del fail-closed: si AuthToken estuviera vacio, la clave HMAC
        seria vacia y predecible -> un atacante podria forjar la firma. Por eso
        rechazamos explicitamente cuando no hay token configurado.
        """
        if not signature or not self._auth_token:
            return False
        expected = self.validator.compute_signature(uri, params)
        return hmac.compare_digest(expected, signature)

    def parse_inbound(self, form: dict[str, Any]) -> InboundMessage:
        """Normaliza prefijo whatsapp: -> E.164 y valida el From del remitente.

        El remitente (From) SIEMPRE llega como E.164 desde Twilio; si no, es un
        payload malformado y se rechaza con ValueError (no se persiste).
        """
        from_number = normalize_phone(form.get("From", ""))
        validate_e164(from_number)
        return InboundMessage(
            from_number=from_number,
            to_number=normalize_phone(form.get("To", "")),
            body=form.get("Body", ""),
            message_sid=form.get("MessageSid", ""),
            profile_name=form.get("ProfileName") or None,
            raw=dict(form),
        )


# Singleton para la app (cliente lazy). DI via get_whatsapp_service().
whatsapp_service = WhatsAppService()


def get_whatsapp_service() -> WhatsAppService:
    """Dependency de FastAPI: devuelve el singleton."""
    return whatsapp_service
