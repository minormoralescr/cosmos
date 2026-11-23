"""Verificacion real end-to-end del pipeline de IA contra z.ai (live).

No es un test del suite (haría red en CI). Lo ejecuto manualmente para probar que:
  prompt -> CLI z-ai -> glm live -> parse (fence-strip) -> Intent
funciona de punta a punta, no solo con fakes.

Uso: PYTHONPATH=. python scripts/verify_ai_integration.py
"""
import asyncio

from app.services.ai_agent_service import AIAgentService, _CliZaiClient, Intent


async def main():
    svc = AIAgentService(client=_CliZaiClient())
    samples = [
        "Hola, quiero reservar un masaje para manana",
        "Tengo una cita el viernes y quiero cambiarla de dia",
        "Necesito cancelar mi reserva del miercoles",
        "Cuanto cuesta el facial y que horarios tienen?",
        "Quiero hablar con una persona, no con un bot",
    ]
    for text in samples:
        intent, conf = await svc.classify_intent(text)
        print(f"[{intent.value:10s} conf={conf:.2f}]  {text}")

    # smoke: el router del webhook importa limpio
    from app.api.webhooks import router
    paths = [r.path for r in router.routes]
    print("webhook routes:", paths)
    assert "/webhooks/whatsapp" in paths


if __name__ == "__main__":
    asyncio.run(main())
