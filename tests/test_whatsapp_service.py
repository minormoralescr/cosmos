"""Tests de whatsapp_service.

Estrategia (Directiva #4: cero simulaciones de logica):
  - normalize_phone / validate_e164 / parse_inbound: logica pura, sin Twilio.
  - verify_webhook_signature: HMAC REAL. El test computa la firma con un
    RequestValidator real y token conocido, y verifica que el servicio la
    acepta (True) y rechaza falsificaciones (False) + fail-closed sin token.
  - send_whatsapp / send_template: mock SOLO en la frontera del Client
    (registra los kwargs con los que se llamo a messages.create). La logica
    de normalizacion, validacion E.164 y armado de la direccion whatsapp:
    se valida de verdad (no se simula el exito).
"""
from __future__ import annotations

import json

import pytest
from twilio.request_validator import RequestValidator

from app.services.whatsapp_service import (
    InboundMessage,
    WhatsAppService,
    normalize_phone,
    validate_e164,
)
from app.config import settings


# --- Frontera: fake Client (unica capa mockeada) ---

class _FakeMessage:
    def __init__(self, sid: str):
        self.sid = sid


class _FakeMessages:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeMessage(f"SMfake{len(self.calls)}")


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


# --- Funciones puras ---

def test_normalize_phone_strips_whatsapp_prefix():
    assert normalize_phone("whatsapp:+34600000001") == "+34600000001"


def test_normalize_phone_leaves_plain_e164():
    assert normalize_phone("+34600000001") == "+34600000001"


def test_validate_e164_accepts_valid():
    # E.164 requiere 6-15 digitos tras '+'
    validate_e164("+34600000001")          # 11 digitos
    validate_e164("+123456")               # 6 digitos (minimo)
    validate_e164("+123456789012345")      # 15 digitos (maximo)


def test_validate_e164_rejects_invalid():
    bad = [
        "34600000001",   # sin '+'
        "+34600",        # 5 digitos (<6)
        "+1234567890123456",  # 16 digitos (>15)
        "+3460abc0001",  # letras
        "",              # vacio
        "whatsapp:+34600000001",  # con prefijo (no normalizado)
    ]
    for phone in bad:
        with pytest.raises(ValueError):
            validate_e164(phone)


# --- parse_inbound ---

def test_parse_inbound_normalizes_and_extracts():
    form = {
        "From": "whatsapp:+34600000001",
        "To": "whatsapp:+14155238886",
        "Body": "Hola, quiero reservar",
        "MessageSid": "SMabc123",
        "ProfileName": "Ana",
    }
    msg = WhatsAppService(auth_token="x").parse_inbound(form)
    assert isinstance(msg, InboundMessage)
    assert msg.from_number == "+34600000001"  # normalizado
    assert msg.to_number == "+14155238886"
    assert msg.body == "Hola, quiero reservar"
    assert msg.message_sid == "SMabc123"
    assert msg.profile_name == "Ana"
    assert msg.raw == form  # preservado para auditoria


def test_parse_inbound_optional_profile_becomes_none():
    form = {"From": "whatsapp:+34600000001", "Body": "x", "MessageSid": "SM1"}
    msg = WhatsAppService(auth_token="x").parse_inbound(form)
    assert msg.profile_name is None


def test_parse_inbound_rejects_non_e164_from():
    # payload malformado: From sin E.164 -> ValueError (no se persiste)
    with pytest.raises(ValueError):
        WhatsAppService(auth_token="x").parse_inbound({"From": "whatsapp:346"})


# --- verify_webhook_signature (HMAC real) ---

_URI = "https://spa.example.com/webhooks/whatsapp"
_PARAMS = {"From": "whatsapp:+34600000001", "Body": "Hola", "MessageSid": "SMabc"}
_TOKEN = "my_test_auth_token"


def _real_signature(token: str, uri: str = _URI, params: dict = _PARAMS) -> str:
    """Firma HMAC-SHA1 real (la misma que Twilio enviaria)."""
    return RequestValidator(token).compute_signature(uri, params)


def test_verify_accepts_real_signature():
    svc = WhatsAppService(auth_token=_TOKEN)
    sig = _real_signature(_TOKEN)
    assert svc.verify_webhook_signature(uri=_URI, params=_PARAMS, signature=sig) is True


def test_verify_rejects_tampered_params():
    svc = WhatsAppService(auth_token=_TOKEN)
    sig = _real_signature(_TOKEN)
    tampered = {**_PARAMS, "Body": "mensaje distinto"}
    assert svc.verify_webhook_signature(uri=_URI, params=tampered, signature=sig) is False


def test_verify_rejects_tampered_signature():
    svc = WhatsAppService(auth_token=_TOKEN)
    sig = _real_signature(_TOKEN)
    bad_sig = sig[:-2] + ("AA" if not sig.endswith("AA") else "BB")
    assert svc.verify_webhook_signature(uri=_URI, params=_PARAMS, signature=bad_sig) is False


def test_verify_rejects_empty_signature():
    svc = WhatsAppService(auth_token=_TOKEN)
    assert svc.verify_webhook_signature(uri=_URI, params=_PARAMS, signature="") is False
    assert svc.verify_webhook_signature(uri=_URI, params=_PARAMS, signature=None) is False


def test_verify_fail_closed_when_token_empty():
    """Sin AuthToken configurado: rechaza incluso una firma 'correcta' forjada
    con la clave vacia (predecible). Garantia anti-falsificacion."""
    forged = _real_signature("")  # firma que un atacante computaria con clave vacia
    svc = WhatsAppService(auth_token="")
    assert svc.verify_webhook_signature(uri=_URI, params=_PARAMS, signature=forged) is False


def test_verify_rejects_wrong_token():
    """Firma computada con otro token -> no coincide."""
    svc = WhatsAppService(auth_token=_TOKEN)
    sig_other_token = _real_signature("otro_token_distinto")
    assert svc.verify_webhook_signature(uri=_URI, params=_PARAMS, signature=sig_other_token) is False


# --- send_whatsapp (frontera Client mockeada; logica real) ---

@pytest.mark.asyncio
async def test_send_whatsapp_normalizes_and_sends_with_prefix():
    fake = _FakeClient()
    svc = WhatsAppService(client=fake, auth_token=_TOKEN)
    sid = await svc.send_whatsapp("whatsapp:+34600000001", "Su cita esta confirmada")
    assert sid == "SMfake1"
    call = fake.messages.calls[0]
    assert call["from_"] == settings.twilio_whatsapp_from
    assert call["to"] == "whatsapp:+34600000001"  # prefijo re-anadido para envio
    assert call["body"] == "Su cita esta confirmada"


@pytest.mark.asyncio
async def test_send_whatsapp_accepts_plain_e164_too():
    fake = _FakeClient()
    svc = WhatsAppService(client=fake, auth_token=_TOKEN)
    await svc.send_whatsapp("+34600000001", "x")
    assert fake.messages.calls[0]["to"] == "whatsapp:+34600000001"


@pytest.mark.asyncio
async def test_send_whatsapp_rejects_invalid_to_before_client():
    fake = _FakeClient()
    svc = WhatsAppService(client=fake, auth_token=_TOKEN)
    with pytest.raises(ValueError):
        await svc.send_whatsapp("346", "x")  # no E.164
    assert fake.messages.calls == []  # el client NO fue llamado


@pytest.mark.asyncio
async def test_send_whatsapp_rejects_empty_body():
    fake = _FakeClient()
    svc = WhatsAppService(client=fake, auth_token=_TOKEN)
    with pytest.raises(ValueError):
        await svc.send_whatsapp("+34600000001", "")
    with pytest.raises(ValueError):
        await svc.send_whatsapp("+34600000001", "   ")
    assert fake.messages.calls == []


# --- send_template ---

@pytest.mark.asyncio
async def test_send_template_serializes_variables_to_json():
    fake = _FakeClient()
    svc = WhatsAppService(client=fake, auth_token=_TOKEN)
    await svc.send_template(
        "+34600000001",
        content_sid="HXabcdef",
        variables={"1": "Ana", "2": "manana 10:00"},
    )
    call = fake.messages.calls[0]
    assert call["content_sid"] == "HXabcdef"
    assert call["to"] == "whatsapp:+34600000001"
    assert json.loads(call["content_variables"]) == {"1": "Ana", "2": "manana 10:00"}


@pytest.mark.asyncio
async def test_send_template_without_variables_omits_field():
    fake = _FakeClient()
    svc = WhatsAppService(client=fake, auth_token=_TOKEN)
    await svc.send_template("+34600000001", content_sid="HXabc")
    call = fake.messages.calls[0]
    assert "content_variables" not in call


@pytest.mark.asyncio
async def test_send_template_rejects_invalid_to():
    fake = _FakeClient()
    svc = WhatsAppService(client=fake, auth_token=_TOKEN)
    with pytest.raises(ValueError):
        await svc.send_template("bad", content_sid="HXabc")
    assert fake.messages.calls == []


@pytest.mark.asyncio
async def test_send_template_rejects_empty_content_sid():
    fake = _FakeClient()
    svc = WhatsAppService(client=fake, auth_token=_TOKEN)
    with pytest.raises(ValueError):
        await svc.send_template("+34600000001", content_sid="")
    assert fake.messages.calls == []


# --- cliente lazy + guard de config ---

def test_client_raises_when_twilio_not_configured():
    # settings por defecto tiene sid/token vacios -> RuntimeError claro
    svc = WhatsAppService()
    with pytest.raises(RuntimeError, match="Twilio no configurado"):
        _ = svc.client
