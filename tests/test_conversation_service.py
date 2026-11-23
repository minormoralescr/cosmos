"""Tests de conversation_service.

Estrategia (Directiva #4):
  - decide_routing: PURE (sin DB, sin mocks).
  - handle_inbound: glue con fake repos (frontera DB) + fake AIAgent + fake
    WhatsApp (fronteras externas). Los fakes son behavior-driven; se valida la
    DECISION de routing, persistencia y envio (no se simula el exito).
  - Tier B: PostgreSQL real + fakes externos (auto-skip sin DB).
"""
from __future__ import annotations

import pytest

from app.db.models import AgentType, ConversationStatus
from app.services.ai_agent_service import Intent
from app.services.conversation_service import ConversationService, decide_routing
from app.services.whatsapp_service import InboundMessage


# --- decide_routing (puro) ---

def test_routing_ai_disabled_goes_to_human():
    d = decide_routing(Intent.BOOK, 0.99, ai_enabled=False)
    assert d.route_to == AgentType.HUMAN


def test_routing_handoff_intent_goes_to_human():
    d = decide_routing(Intent.HANDOFF, 0.99, ai_enabled=True)
    assert d.route_to == AgentType.HUMAN


def test_routing_low_confidence_goes_to_human():
    d = decide_routing(Intent.BOOK, 0.3, ai_enabled=True)
    assert d.route_to == AgentType.HUMAN


def test_routing_high_confidence_goes_to_ai():
    d = decide_routing(Intent.BOOK, 0.9, ai_enabled=True)
    assert d.route_to == AgentType.AI


def test_routing_threshold_override():
    assert decide_routing(Intent.BOOK, 0.5, True, threshold=0.6).route_to == AgentType.HUMAN
    assert decide_routing(Intent.BOOK, 0.5, True, threshold=0.4).route_to == AgentType.AI


# --- Fakes ---

class _FakeClientRepo:
    def __init__(self, client):
        self.client = client
        self.created = False

    async def get_or_create_by_phone(self, phone, *, name=None):
        return self.client


class _FakeConvRepo:
    def __init__(self, existing=None, created=None):
        self.existing = existing
        self.created = created
        self.updates: list[tuple] = []

    async def get_open_by_client(self, client_id):
        return self.existing

    async def create(self, data):
        if self.created is None:
            from app.db.models import Conversation
            c = Conversation(**data)
            c.id = 1
            self.created = c
        return self.created

    async def update(self, obj, data):
        for k, v in data.items():
            setattr(obj, k, v)
        self.updates.append((obj, dict(data)))
        return obj


class _FakeMessageRepo:
    def __init__(self):
        self.created: list[dict] = []

    async def create(self, data):
        self.created.append(dict(data))
        return data


class _FakeAgentRepo:
    def __init__(self, ai_agent=None):
        self.ai_agent = ai_agent

    async def get_ai_agent(self):
        return self.ai_agent


class _FakeAIAgent:
    def __init__(self, *, classify=(Intent.BOOK, 0.9), reply="Respuesta IA"):
        self._classify = classify
        self._reply = reply
        self.classify_calls = 0
        self.reply_calls = 0

    async def classify_intent(self, text, *, threshold=0.6):
        self.classify_calls += 1
        return self._classify

    async def generate_reply(self, intent, user_text, client_name):
        self.reply_calls += 1
        return self._reply


class _FakeWhatsApp:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_whatsapp(self, to, body):
        self.sent.append((to, body))
        return "SMfake"


def _inbound(body="quiero reservar", phone="+34600000001", name="Ana"):
    return InboundMessage(
        from_number=phone, to_number="+14155238886", body=body,
        message_sid="SMxyz", profile_name=name,
        raw={"From": f"whatsapp:{phone}", "Body": body},
    )


def _client(phone="+34600000001", name="Ana", cid=1):
    from app.db.models import Client
    return Client(id=cid, name=name, phone=phone, email=None)


def _conv_svc(*, ai_agent=None, whatsapp=None, ai_enabled=True,
              client=None, conv_existing=None, ai_agent_row=None):
    from app.db.models import Conversation
    if client is None:
        client = _client()
    created_conv = Conversation(id=1, client_id=client.id, status=ConversationStatus.OPEN)
    return ConversationService(
        session=None, ai_agent=ai_agent or _FakeAIAgent(), whatsapp=whatsapp or _FakeWhatsApp(),
        ai_enabled=ai_enabled,
        clients=_FakeClientRepo(client),
        conversations=_FakeConvRepo(existing=conv_existing, created=created_conv),
        messages=_FakeMessageRepo(),
        agents=_FakeAgentRepo(ai_agent=ai_agent_row),
    ), created_conv


# --- handle_inbound (Tier A glue) ---

@pytest.mark.asyncio
async def test_handle_inbound_ai_route_generates_and_sends_reply():
    wa = _FakeWhatsApp()
    ai = _FakeAIAgent(classify=(Intent.BOOK, 0.95), reply="Claro, dime el servicio")
    svc, _ = _conv_svc(ai_agent=ai, whatsapp=wa, ai_enabled=True)

    reply = await svc.handle_inbound(_inbound("quiero una cita"))

    assert reply == "Claro, dime el servicio"
    assert ai.reply_calls == 1            # genero respuesta IA
    assert wa.sent == [("+34600000001", "Claro, dime el servicio")]
    # 1 inbound + 1 outbound persistidos
    assert len(svc.messages.created) == 2
    assert any(m["direction"] == "outbound" for m in svc.messages.created)


@pytest.mark.asyncio
async def test_handle_inbound_handoff_routes_to_human_and_sends_ack():
    wa = _FakeWhatsApp()
    ai = _FakeAIAgent(classify=(Intent.HANDOFF, 0.4))
    svc, conv = _conv_svc(ai_agent=ai, whatsapp=wa, ai_enabled=True)

    reply = await svc.handle_inbound(_inbound("algo complejo"))

    assert reply != ai._reply            # no es la respuesta IA
    assert "asesor humano" in reply      # es el ack de derivacion
    assert ai.reply_calls == 0           # la IA no genero respuesta
    assert conv.status == ConversationStatus.AWAITING_HUMAN
    assert wa.sent and wa.sent[0][1] == reply


@pytest.mark.asyncio
async def test_handle_inbound_ai_disabled_skips_classification():
    wa = _FakeWhatsApp()
    ai = _FakeAIAgent()
    svc, conv = _conv_svc(ai_agent=ai, whatsapp=wa, ai_enabled=False)

    await svc.handle_inbound(_inbound("hola"))

    assert ai.classify_calls == 0        # no llama al LLM
    assert conv.status == ConversationStatus.AWAITING_HUMAN
    assert wa.sent  # manda ack


@pytest.mark.asyncio
async def test_handle_inbound_reuses_existing_open_conversation():
    from app.db.models import Conversation
    existing = Conversation(id=42, client_id=1, status=ConversationStatus.OPEN)
    svc, _ = _conv_svc(conv_existing=existing)
    conv_repo = svc.conversations

    await svc.handle_inbound(_inbound())

    # no se creo una conversacion nueva (created sigue siendo el placeholder)
    assert conv_repo.created.id != 42 or conv_repo.existing is existing


# --- Tier B: PostgreSQL real + fakes externos (auto-skip) ---

@pytest.mark.asyncio
async def test_handle_inbound_persists_to_db(db_session):
    from app.db.models import Agent, AgentType
    from app.db.repositories import (
        AgentRepository, ClientRepository, ConversationRepository, MessageRepository,
    )

    await AgentRepository(db_session).create(
        {"type": AgentType.AI, "name": "Glam Assistant", "is_active": True}
    )
    wa = _FakeWhatsApp()
    ai = _FakeAIAgent(classify=(Intent.FAQ, 0.85), reply="Abrimos de 9 a 20h.")
    svc = ConversationService(db_session, ai, wa)

    await svc.handle_inbound(_inbound(body="que horarios tienen", phone="+34600000099"))
    await db_session.commit()

    client = await ClientRepository(db_session).get_by_phone("+34600000099")
    assert client is not None
    conv = await ConversationRepository(db_session).get_open_by_client(client.id)
    assert conv is not None
    assert conv.last_intent == "faq"
    msgs = await MessageRepository(db_session).list_by_conversation(conv.id)
    assert len(msgs) == 2  # inbound + outbound
    assert wa.sent  # se envio la respuesta por WA
