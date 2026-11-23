"""AI agent service — clasificador de intencion + generador de respuesta (z.ai / glm).

Pipeline de clasificacion:
  1. build_intent_prompt(text) -> (system, user)         [puro]
  2. zai_client.chat(system, user) -> contenido crudo    [frontera externa]
  3. parse_intent_json(content) -> (Intent, confidence)  [puro: extrae JSON
     entre la primera '{' y la ultima '}' -> tolera fences ```json y prosa]
  4. classify_intent(text) -> (Intent, confidence)       [fail-safe -> HANDOFF]

Frontera inyectable ZaiClient:
  - _HttpZaiClient: PROD (config zai_base_url/zai_api_key/zai_model, OpenAI-compat).
  - _CliZaiClient: dev/integracion (subprocess `z-ai chat`, verificado en sandbox;
    util cuando no hay ZAI_API_KEY). El CLI usa glm-4-plus (no permite elegir modelo).
  - make_zai_client(): elige HTTP si hay key, sino CLI si `z-ai` esta en PATH.

Mocks: SOLO el cliente z.ai (frontera externa). La logica de prompt, parsing,
validacion de intencion/confianza y umbral es real y pura.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from enum import StrEnum
from typing import Protocol

import httpx

from app.config import settings

# Umbral de confianza: por debajo -> derivar a humano (fail-safe).
CONFIDENCE_THRESHOLD = 0.6

_FALLBACK_REPLY = (
    "Gracias por tu mensaje. En breve te respondemos. 🧖"
)

_INTENT_SYSTEM_PROMPT = (
    "Eres un clasificador de intenciones para un spa por WhatsApp. Dado el "
    "mensaje del cliente, respondes SOLO con un JSON valido, sin texto adicional: "
    '{"intent": "<intencion>", "confidence": <0.0-1.0>}. '
    'Intenciones permitidas: "book" (reservar cita), "reschedule" (reprogramar), '
    '"cancel" (cancelar), "faq" (pregunta sobre servicios/precios/horarios), '
    '"handoff" (fuera de alcance o requiere humano). '
    "confidence refleja tu certeza (0.0=nada seguro, 1.0=total)."
)


class Intent(StrEnum):
    BOOK = "book"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    FAQ = "faq"
    HANDOFF = "handoff"

    @classmethod
    def allowed(cls) -> frozenset[str]:
        return frozenset(i.value for i in cls)


# --- Cliente z.ai (frontera) ---

class ZaiClient(Protocol):
    """Frontera del backend LLM. Devuelve el contenido del mensaje del asistente."""

    async def chat(self, system: str, user: str) -> str: ...


class _HttpZaiClient:
    """Prod: HTTP OpenAI-compatible a {zai_base_url}/chat/completions.

    Sigue la convencion del SDK Node (rol 'assistant' para el system prompt).
    Requiere ZAI_API_KEY configurado.
    """

    def __init__(
        self,
        base_url: str = settings.zai_base_url,
        api_key: str = settings.zai_api_key,
        model: str = settings.zai_model,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    async def chat(self, system: str, user: str) -> str:
        if not self._api_key:
            raise RuntimeError("z.ai no configurado: set ZAI_API_KEY")
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "assistant", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "thinking": {"type": "disabled"},
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]


class _CliZaiClient:
    """Dev/integracion: subprocess `z-ai chat`. Verificado en sandbox.

    Salida OpenAI-compatible a un fichero temporal; se extrae choices[0].content.
    El CLI no permite elegir modelo (usa glm-4-plus por defecto).
    """

    async def chat(self, system: str, user: str) -> str:
        fd, out_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            proc = await asyncio.create_subprocess_exec(
                "z-ai", "chat", "-p", user, "-s", system, "-o", out_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"z-ai chat fallo (rc={proc.returncode}): "
                    f"{stderr.decode(errors='replace')[:200]}"
                )
            with open(out_path, encoding="utf-8") as f:
                return json.load(f)["choices"][0]["message"]["content"]
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass


def make_zai_client() -> ZaiClient:
    """Selecciona el cliente: HTTP si hay key, sino CLI si `z-ai` en PATH."""
    if settings.zai_api_key:
        return _HttpZaiClient()
    if shutil.which("z-ai"):
        return _CliZaiClient()
    return _HttpZaiClient()  # sin key -> al usarse lanza RuntimeError claro


# --- Logica pura ---

def build_intent_prompt(text: str) -> tuple[str, str]:
    """Construye (system, user) para la clasificacion de intencion."""
    return _INTENT_SYSTEM_PROMPT, text


def parse_intent_json(content: str) -> tuple[Intent, float]:
    """Extrae JSON {intent, confidence} del contenido crudo del LLM.

    Tolera fences ```json ... ``` y prosa envolvente: toma el substring entre la
    primera '{' y la ultima '}'. Valida intent permitido y confidence en [0,1].
    Lanza ValueError si no se puede parsear o validar.
    """
    if not content:
        raise ValueError("contenido vacio")
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"sin objeto JSON en: {content!r}")
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON invalido: {exc}") from exc
    if not isinstance(data, dict) or "intent" not in data or "confidence" not in data:
        raise ValueError(f"faltan campos intent/confidence: {data!r}")
    raw_intent = data["intent"]
    if not isinstance(raw_intent, str) or raw_intent not in Intent.allowed():
        raise ValueError(f"intencion no permitida: {raw_intent!r}")
    try:
        confidence = float(data["confidence"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"confidence invalida: {data['confidence']!r}") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence fuera de rango [0,1]: {confidence}")
    return Intent(raw_intent), confidence


def build_reply_prompt(intent: Intent, user_text: str, client_name: str | None) -> tuple[str, str]:
    """Construye (system, user) para generar la respuesta natural al cliente."""
    name = client_name or "cliente"
    system = (
        "Eres el asistente de WhatsApp de un spa. Respondes en español, breve y "
        "amable (maximo 2 frases). No inventes precios ni horarios exactos; si "
        "falta informacion para reservar o reprogramar, pide servicio y dia/hora."
    )
    user = (
        f"Cliente: {name}. Intencion detectada: {intent.value}. "
        f"Mensaje del cliente: \"{user_text}\". Escribe tu respuesta."
    )
    return system, user


# --- Servicio ---

class AIAgentService:
    def __init__(self, client: ZaiClient | None = None) -> None:
        self._client = client

    @property
    def client(self) -> ZaiClient:
        if self._client is None:
            self._client = make_zai_client()
        return self._client

    async def classify_intent(
        self, text: str, *, threshold: float = CONFIDENCE_THRESHOLD
    ) -> tuple[Intent, float]:
        """Clasifica la intencion del texto. Fail-safe: ante cualquier error o
        confianza baja, devuelve HANDOFF (derivar a humano)."""
        try:
            system, user = build_intent_prompt(text)
            content = await self.client.chat(system, user)
            intent, confidence = parse_intent_json(content)
        except Exception:
            return Intent.HANDOFF, 0.0
        if confidence < threshold:
            return Intent.HANDOFF, confidence
        return intent, confidence

    async def generate_reply(
        self, intent: Intent, user_text: str, client_name: str | None
    ) -> str:
        """Genera una respuesta natural via LLM. Fallback si falla."""
        try:
            system, user = build_reply_prompt(intent, user_text, client_name)
            return await self.client.chat(system, user)
        except Exception:
            return _FALLBACK_REPLY


# Singleton para la app (cliente lazy via make_zai_client). DI via get_ai_agent_service.
ai_agent_service = AIAgentService()


def get_ai_agent_service() -> AIAgentService:
    """Dependency de FastAPI: devuelve el singleton."""
    return ai_agent_service
