"""Tests de ai_agent_service.

Estrategia (Directiva #4):
  - parse_intent_json / build_*_prompt: PURE (sin cliente, sin red).
  - classify_intent / generate_reply: fake ZaiClient (frontera externa). La logica
    de prompt + parsing + umbral + fail-safe es real.
  El fake devuelve contenido realista, incluido el formato ```json``` que produce
  el modelo glm real (verificado con el CLI z-ai en sandbox).
"""
from __future__ import annotations

import pytest

from app.services.ai_agent_service import (
    CONFIDENCE_THRESHOLD,
    AIAgentService,
    Intent,
    build_intent_prompt,
    build_reply_prompt,
    parse_intent_json,
)


# --- Fakes (frontera z.ai) ---

class _FakeZaiClient:
    def __init__(self, content: str = ""):
        self.content = content
        self.calls: list[tuple[str, str]] = []

    async def chat(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.content


class _RaisingZaiClient:
    async def chat(self, system: str, user: str) -> str:
        raise RuntimeError("z.ai caido")


# --- parse_intent_json (puro) ---

def test_parse_plain_json():
    assert parse_intent_json('{"intent":"book","confidence":0.9}') == (Intent.BOOK, 0.9)


def test_parse_fence_wrapped_json_real_zai_format():
    # el modelo glm real envuelve el JSON en fences ```json ... ``` (verificado)
    raw = '```json\n{"intent":"faq","confidence":0.8}\n```'
    assert parse_intent_json(raw) == (Intent.FAQ, 0.8)


def test_parse_with_surrounding_prose():
    raw = 'Sure! {"intent":"cancel","confidence":0.7} hope that helps'
    assert parse_intent_json(raw) == (Intent.CANCEL, 0.7)


def test_parse_whitespace_tolerant():
    assert parse_intent_json('  {"intent":"book","confidence":1.0}  ') == (Intent.BOOK, 1.0)


def test_parse_confidence_int_coerced_to_float():
    assert parse_intent_json('{"intent":"book","confidence":1}') == (Intent.BOOK, 1.0)


@pytest.mark.parametrize("bad", [
    '{"intent":"unknown","confidence":0.9}',   # intent no permitido
    '{"intent":"book"}',                         # sin confidence
    '{"confidence":0.9}',                        # sin intent
    '{"intent":"book","confidence":1.5}',        # confidence > 1
    '{"intent":"book","confidence":-0.1}',       # confidence < 0
    '{"intent":',                                # JSON malformado
    'hello world',                               # sin llaves
    '',                                          # vacio
])
def test_parse_rejects_invalid(bad):
    with pytest.raises(ValueError):
        parse_intent_json(bad)


# --- build_intent_prompt / build_reply_prompt (puros) ---

def test_build_intent_prompt():
    system, user = build_intent_prompt("quiero reservar un masaje")
    assert user == "quiero reservar un masaje"
    # el system lista todas las intenciones permitidas
    for intent in Intent:
        assert intent.value in system


def test_build_reply_prompt_carries_intent_and_name():
    system, user = build_reply_prompt(Intent.BOOK, "hola", "Ana")
    assert "book" in user
    assert "Ana" in user


# --- classify_intent (fake client) ---

@pytest.mark.asyncio
async def test_classify_high_confidence():
    svc = AIAgentService(client=_FakeZaiClient('{"intent":"book","confidence":0.95}'))
    intent, conf = await svc.classify_intent("quiero una cita")
    assert intent == Intent.BOOK
    assert conf == 0.95


@pytest.mark.asyncio
async def test_classify_fence_wrapped_response():
    svc = AIAgentService(
        client=_FakeZaiClient('```json\n{"intent":"faq","confidence":0.9}\n```')
    )
    intent, conf = await svc.classify_intent("que horarios tienen")
    assert intent == Intent.FAQ
    assert conf == 0.9


@pytest.mark.asyncio
async def test_classify_low_confidence_routes_to_handoff():
    svc = AIAgentService(client=_FakeZaiClient('{"intent":"book","confidence":0.3}'))
    intent, conf = await svc.classify_intent("mmh")
    assert intent == Intent.HANDOFF
    assert conf == 0.3  # se preserva la confianza real para auditoria


@pytest.mark.asyncio
async def test_classify_client_error_is_handoff_fail_safe():
    svc = AIAgentService(client=_RaisingZaiClient())
    intent, conf = await svc.classify_intent("hola")
    assert intent == Intent.HANDOFF
    assert conf == 0.0


@pytest.mark.asyncio
async def test_classify_garbage_response_is_handoff():
    svc = AIAgentService(client=_FakeZaiClient("no soy json"))
    intent, conf = await svc.classify_intent("hola")
    assert intent == Intent.HANDOFF
    assert conf == 0.0


@pytest.mark.asyncio
async def test_classify_threshold_override():
    svc = AIAgentService(client=_FakeZaiClient('{"intent":"book","confidence":0.5}'))
    # umbral por defecto (0.6) -> handoff; umbral bajo (0.4) -> book
    assert (await svc.classify_intent("x"))[0] == Intent.HANDOFF
    assert (await svc.classify_intent("x", threshold=0.4))[0] == Intent.BOOK


def test_default_threshold_is_sensible():
    assert 0.5 <= CONFIDENCE_THRESHOLD <= 0.8


# --- generate_reply (fake client) ---

@pytest.mark.asyncio
async def test_generate_reply_returns_llm_content():
    client = _FakeZaiClient("Claro, dime que servicio y dia prefieres.")
    svc = AIAgentService(client=client)
    reply = await svc.generate_reply(Intent.BOOK, "quiero cita", "Ana")
    assert reply == "Claro, dime que servicio y dia prefieres."
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_generate_reply_fallback_on_error():
    svc = AIAgentService(client=_RaisingZaiClient())
    reply = await svc.generate_reply(Intent.FAQ, "hola", None)
    assert "Gracias" in reply  # fallback humanizado
