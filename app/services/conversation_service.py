"""Conversation service — orquesta inbound WhatsApp -> routing -> respuesta.

handle_inbound(InboundMessage):
  1. get_or_create cliente por telefono (E.164 ya normalizado por whatsapp_service)
  2. conversacion OPEN existente o crea una nueva
  3. persiste mensaje inbound (WHATSAPP / INBOUND)
  4. routing: decide_routing(intent, confidence, ai_enabled) -> AI | HUMAN
  5. AI: genera respuesta via LLM, persiste outbound, envia por WA
     HUMAN: marca AWAITING_HUMAN, envia ack de derivacion
  6. devuelve el texto de la respuesta (o None si no se envio nada)

decide_routing es puro (testeable sin DB). handle_inbound usa repos (frontera DB,
inyectables) + ai_agent + whatsapp (fronteras externas, inyectables).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    Agent,
    AgentType,
    Client,
    Conversation,
    ConversationStatus,
    MessageChannel,
    MessageDirection,
)
from app.db.repositories import (
    AgentRepository,
    ClientRepository,
    ConversationRepository,
    MessageRepository,
)
from app.services.ai_agent_service import CONFIDENCE_THRESHOLD, AIAgentService, Intent
from app.services.whatsapp_service import InboundMessage, WhatsAppService

_HANDOFF_ACK = (
    "Gracias por tu mensaje. Te estoy conectando con un asesor humano, "
    "en un momento te atenderemos. 🧖"
)


@dataclass(frozen=True)
class RoutingDecision:
    """Resultado del enrutamiento: a quien va el mensaje y por que."""
    route_to: AgentType
    intent: Intent
    confidence: float


def decide_routing(
    intent: Intent,
    confidence: float,
    ai_enabled: bool,
    *,
    threshold: float = CONFIDENCE_THRESHOLD,
) -> RoutingDecision:
    """Pure: decide si atiende la IA o deriva a humano.

    - IA desactivada -> HUMAN
    - intencion HANDOFF -> HUMAN
    - confianza < umbral -> HUMAN (fail-safe)
    - resto -> AI
    """
    if not ai_enabled or intent == Intent.HANDOFF or confidence < threshold:
        return RoutingDecision(AgentType.HUMAN, intent, confidence)
    return RoutingDecision(AgentType.AI, intent, confidence)


class ConversationService:
    def __init__(
        self,
        session: AsyncSession,
        ai_agent: AIAgentService,
        whatsapp: WhatsAppService,
        *,
        ai_enabled: bool | None = None,
        clients: ClientRepository | None = None,
        conversations: ConversationRepository | None = None,
        messages: MessageRepository | None = None,
        agents: AgentRepository | None = None,
    ) -> None:
        self.session = session
        self.ai_agent = ai_agent
        self.whatsapp = whatsapp
        self.ai_enabled = settings.ai_agent_enabled if ai_enabled is None else ai_enabled
        self.clients = clients or ClientRepository(session)
        self.conversations = conversations or ConversationRepository(session)
        self.messages = messages or MessageRepository(session)
        self.agents = agents or AgentRepository(session)

    async def handle_inbound(self, inbound: InboundMessage) -> str | None:
        """Procesa un inbound: routing + persistencia + envio de respuesta."""
        client = await self.clients.get_or_create_by_phone(
            inbound.from_number, name=inbound.profile_name
        )
        conv = await self._get_or_open_conversation(client)
        await self.messages.create({
            "conversation_id": conv.id,
            "direction": MessageDirection.INBOUND,
            "channel": MessageChannel.WHATSAPP,
            "body": inbound.body,
            "twilio_message_sid": inbound.message_sid,
        })

        intent, confidence = await self._classify(inbound.body)
        decision = decide_routing(intent, confidence, self.ai_enabled)
        await self.conversations.update(conv, {
            "last_intent": decision.intent.value,
            "ai_confidence": decision.confidence,
        })

        if decision.route_to == AgentType.HUMAN:
            await self.conversations.update(
                conv, {"status": ConversationStatus.AWAITING_HUMAN}
            )
            await self._reply(conv, client, _HANDOFF_ACK)
            return _HANDOFF_ACK

        reply = await self.ai_agent.generate_reply(decision.intent, inbound.body, client.name)
        await self._reply(conv, client, reply)
        return reply

    async def _get_or_open_conversation(self, client: Client) -> Conversation:
        conv = await self.conversations.get_open_by_client(client.id)
        if conv is not None:
            return conv
        ai_agent = await self.agents.get_ai_agent()
        return await self.conversations.create({
            "client_id": client.id,
            "status": ConversationStatus.OPEN,
            "assigned_agent_id": ai_agent.id if ai_agent else None,
        })

    async def _classify(self, body: str) -> tuple[Intent, float]:
        if not self.ai_enabled:
            return Intent.FAQ, 0.0  # no clasifica; decide_routing lo manda a HUMAN
        return await self.ai_agent.classify_intent(body)

    async def _reply(self, conv: Conversation, client: Client, body: str) -> None:
        """Persiste el mensaje outbound + lo envia por WhatsApp."""
        await self.messages.create({
            "conversation_id": conv.id,
            "direction": MessageDirection.OUTBOUND,
            "channel": MessageChannel.WHATSAPP,
            "body": body,
        })
        await self.whatsapp.send_whatsapp(client.phone, body)

    async def send_human_reply(
        self, conversation_id: int, body: str, *, sender_agent_id: int | None = None
    ) -> Conversation:
        """Respuesta manual de un operador: persiste el mensaje outbound, lo envia
        por WhatsApp y, si la conversacion estaba AWAITING_HUMAN, la reabre (OPEN)
        asignandola al agente humano que responde."""
        conv = await self.conversations.get(conversation_id)
        if conv is None:
            raise ValueError(f"Conversacion {conversation_id} no existe")
        client = await self.clients.get(conv.client_id)
        if client is None:
            raise ValueError(f"Cliente de la conversacion {conversation_id} no existe")
        await self.messages.create({
            "conversation_id": conv.id,
            "direction": MessageDirection.OUTBOUND,
            "channel": MessageChannel.WHATSAPP,
            "body": body,
        })
        await self.whatsapp.send_whatsapp(client.phone, body)
        if conv.status == ConversationStatus.AWAITING_HUMAN:
            await self.conversations.update(conv, {
                "status": ConversationStatus.OPEN,
                "assigned_agent_id": sender_agent_id,
            })
        return conv
