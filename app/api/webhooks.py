"""Webhook de Twilio WhatsApp — unico punto de entrada de mensajes inbound.

POST /webhooks/whatsapp:
  1. verifica la firma HMAC-SHA1 (X-Twilio-Signature) -> 403 si invalida
  2. parsea el inbound (normaliza whatsapp: -> E.164)
  3. delega a ConversationService.handle_inbound (routing + persistencia + reply)
  4. commitea la transaccion y responde 204

Sin auth JWT (es webhook publico de Twilio) pero FIRMADO: solo Twilio conoce el
AuthToken y puede producir una firma valida. Detras de un proxy TLS-terminador,
la URL firmada por Twilio debe coincidir con la que ve FastAPI (X-Forwarded-Proto).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.services.ai_agent_service import AIAgentService, get_ai_agent_service
from app.services.conversation_service import ConversationService
from app.services.whatsapp_service import WhatsAppService, get_whatsapp_service

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/whatsapp")
async def twilio_whatsapp_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    whatsapp: WhatsAppService = Depends(get_whatsapp_service),
    ai_agent: AIAgentService = Depends(get_ai_agent_service),
) -> Response:
    # Form params completos (la firma se computa sobre TODOS los params, ordenados).
    form = await request.form()
    params = {k: v for k, v in form.items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    uri = str(request.url)

    if not whatsapp.verify_webhook_signature(uri=uri, params=params, signature=signature):
        raise HTTPException(status_code=403, detail="Firma de webhook invalida")

    inbound = whatsapp.parse_inbound(params)
    conv_svc = ConversationService(session, ai_agent, whatsapp)
    await conv_svc.handle_inbound(inbound)
    await session.commit()
    return Response(status_code=204)
